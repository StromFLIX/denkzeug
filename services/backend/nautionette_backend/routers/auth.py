"""Human account operations. Deliberately excluded from the agent MCP catalog."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import auth, auth_mail
from ..auth_store import (
    audit,
    email,
    fail,
    issue_token,
    public_user,
    store,
    throttle,
    verify_password,
)
from ..security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Principal,
    require_admin,
    require_recent,
    require_user,
    secure_cookies,
    validate_origin,
    validate_public_origin,
)

router = APIRouter(prefix="/api/auth", tags=["Accounts"])
User = Annotated[Principal, Depends(require_user)]
Admin = Annotated[Principal, Depends(require_admin)]
Password = Annotated[str, Field(min_length=1, max_length=1024)]
Token = Annotated[str, Field(min_length=1, max_length=512)]
Address = Annotated[str, Field(min_length=3, max_length=254)]


class Form(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Form):
    username: str = Field(min_length=1, max_length=64)
    password: Password
    native: bool = False


class Bootstrap(Login):
    token: Token
    email: str | None = Field(default=None, max_length=254)


class Refresh(Form):
    refresh_token: Token


class Logout(Form):
    all_devices: bool = False


class InvitationLookup(Form):
    code: Token


class Registration(Form):
    code: Token
    username: str = Field(min_length=3, max_length=64)
    password: Password
    email: str | None = Field(default=None, max_length=254)
    native: bool = False


class TokenForm(Form):
    token: Token


class Forgot(Form):
    email: Address


class Reset(TokenForm):
    password: Password


class PasswordChange(Form):
    current_password: Password
    password: Password


class Reauthentication(Form):
    password: Password


class Profile(Form):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=254)
    current_password: Password | None = None


class UserChange(Form):
    role: Literal["admin", "member"] | None = None
    status: Literal["active", "suspended"] | None = None


class Invitation(Form):
    email: str | None = Field(default=None, max_length=254)
    label: str = Field(default="", max_length=100)
    role: Literal["admin", "member"] = "member"
    expires_in_days: int = Field(default=7, ge=1, le=30)
    send_email: bool = False


class Policy(Form):
    registration_mode: Literal["paused", "invite_only"]


def rate(request: Request, action: str, identity: str = "", per_identity: int = 8) -> None:
    peer = request.client.host if request.client else "unknown"
    throttle(f"{action}:ip:{peer}", 60)
    if identity:
        throttle(f"{action}:identity:{identity.strip().lower()}", per_identity)


def clear_cookies(response: Response) -> None:
    for key in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            key, path="/", secure=secure_cookies(), httponly=key == SESSION_COOKIE, samesite="lax"
        )


def _invitation(row) -> dict:
    status = (
        "revoked"
        if row["revoked_at"]
        else "accepted"
        if row["consumed_at"]
        else "expired"
        if row["expires_at"] <= time.time()
        else "pending"
    )
    return {
        **{key: row[key] for key in ("id", "email", "role", "label", "created_at", "expires_at")},
        "status": status,
    }


@router.get("/meta")
def metadata() -> dict:
    return {
        "setup_required": auth.setup_required(),
        "registration_mode": store().one("SELECT registration_mode FROM auth_policy WHERE id=1")[
            "registration_mode"
        ],
        "mail_available": auth_mail.available(),
        "providers": [],
        "native_min_version": 1,
    }


@router.post("/bootstrap")
def bootstrap(body: Bootstrap, request: Request, response: Response) -> dict:
    (validate_public_origin if body.native else validate_origin)(request)
    rate(request, "bootstrap")
    user = auth.bootstrap(body.token, body.username, body.email, body.password)
    return auth.create_session(user, request, response, body.native)


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> dict:
    # Native WebViews have explicitly configured origins and use bearer sessions,
    # never cross-origin browser cookies.
    (validate_public_origin if body.native else validate_origin)(request)
    rate(request, "login", body.username)
    user = auth.authenticate(body.username, body.password)
    return auth.create_session(user, request, response, body.native)


@router.get("/me")
def me(request: Request, principal: User) -> dict:
    row = store().one("SELECT * FROM auth_users WHERE id=?", (principal.user_id,))
    csrf = "" if request.state.auth_session["native"] else request.cookies.get(CSRF_COOKIE, "")
    return {"user": public_user(row), "session_id": principal.session_id, "csrf_token": csrf}


@router.post("/refresh")
def refresh(body: Refresh, request: Request) -> dict:
    rate(request, "refresh")
    return auth.rotate_refresh(body.refresh_token)


@router.post("/logout")
def logout(request: Request, response: Response, principal: User, body: Logout | None = None) -> dict:
    with store().transaction() as conn:
        if body and body.all_devices:
            auth.revoke(conn, principal.user_id)
        else:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE id=?", (time.time(), principal.session_id)
            )
        audit(conn, "session.signed_out", principal.user_id, principal.user_id)
    clear_cookies(response)
    return {"signed_out": True}


@router.post("/invitation")
def inspect_invitation(body: InvitationLookup, request: Request) -> dict:
    # POST keeps the invitation secret out of reverse-proxy/access-log URLs.
    rate(request, "invite.inspect")
    return auth.invitation(body.code)


@router.post("/register")
def register(body: Registration, request: Request, response: Response) -> dict:
    validate_public_origin(request)
    rate(request, "register")
    user = auth.register(body.code, body.username, body.password, body.email or None)
    return {
        **auth.create_session(user, request, response, body.native),
        "verification_required": False,
        "email_verification_pending": bool(user["email"] and not user["email_verified"]),
    }


@router.post("/verify-email")
def verify_email(body: TokenForm, request: Request) -> dict:
    rate(request, "verify_email")
    auth.verify_email(body.token)
    return {"verified": True}


@router.post("/forgot-password")
def forgot_password(body: Forgot, request: Request) -> dict:
    validate_public_origin(request)
    address = email(body.email)
    rate(request, "recovery", address, per_identity=3)
    auth.forgot_password(address)
    return {
        "message": "If an account matches this email, a recovery link is on its way. "
        "Check your inbox and spam folder."
    }


@router.post("/reset-password")
def reset_password(body: Reset, request: Request, response: Response) -> dict:
    validate_public_origin(request)
    rate(request, "reset")
    auth.reset_password(body.token, body.password)
    clear_cookies(response)
    return {"reset": True, "message": "Password reset. Sign in with your new password."}


@router.post("/change-password")
def change_password(body: PasswordChange, request: Request, response: Response, principal: User) -> dict:
    rate(request, "password.change", principal.user_id)
    auth.change_password(principal, body.current_password, body.password)
    clear_cookies(response)
    return {"changed": True, "signed_out": True}


@router.post("/reauth")
def reauthenticate(body: Reauthentication, request: Request, principal: User) -> dict:
    rate(request, "reauth", principal.user_id)
    user = store().one("SELECT * FROM auth_users WHERE id=?", (principal.user_id,))
    if not verify_password(user["password_hash"], body.password):
        fail(403, "invalid_credentials", "The password is incorrect.")
    store().execute("UPDATE auth_sessions SET reauth_at=? WHERE id=?", (time.time(), principal.session_id))
    return {"authenticated": True}


@router.patch("/profile")
def profile(body: Profile, request: Request, principal: User) -> dict:
    address = email(body.email) if body.email else None
    user = store().one("SELECT * FROM auth_users WHERE id=?", (principal.user_id,))
    changing_email = "email" in body.model_fields_set and address != user["email"]
    if changing_email:
        rate(request, "email.change", principal.user_id, 3)
        require_recent(request)
        if not body.current_password or not verify_password(user["password_hash"], body.current_password):
            fail(403, "invalid_credentials", "Confirm your current password to change your email.")
    with store().transaction() as conn:
        current = conn.execute(
            "SELECT status,password_hash FROM auth_users WHERE id=?", (principal.user_id,)
        ).fetchone()
        if not current or current["status"] != "active" or current["password_hash"] != user["password_hash"]:
            fail(409, "account_changed", "Please sign in again before changing your profile.")
        if body.display_name is not None:
            if not body.display_name.strip() or any(ord(char) < 32 for char in body.display_name):
                fail(422, "invalid_display_name", "Enter a valid display name.")
            conn.execute(
                "UPDATE auth_users SET display_name=?,updated_at=? WHERE id=?",
                (body.display_name.strip(), time.time(), principal.user_id),
            )
        if changing_email:
            if conn.execute("SELECT 1 FROM auth_users WHERE email=?", (address,)).fetchone():
                fail(409, "account_conflict", "This email is unavailable.")
            conn.execute(
                "UPDATE auth_tokens SET revoked_at=? WHERE user_id=? "
                "AND purpose IN ('change_email','verify_email','reset')",
                (time.time(), principal.user_id),
            )
            conn.execute(
                "UPDATE auth_users SET email=?,email_verified=0,updated_at=? WHERE id=?",
                (address, time.time(), principal.user_id),
            )
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND id!=?",
                (time.time(), principal.user_id, principal.session_id),
            )
            if user["email_verified"]:
                auth_mail.notify(
                    conn,
                    user["email"],
                    "Your optional Denkzeug recovery email changed",
                    "Your account recovery email was changed or removed. Contact your administrator "
                    "immediately if you did not make this change.",
                )
        audit(conn, "profile.updated", principal.user_id, principal.user_id)
    if changing_email and address and auth_mail.available():
        try:
            auth.request_email_verification(principal.user_id)
        except (OSError, ValueError, HTTPException):
            pass  # The optional address was saved; retry verification from Profile & security.
    return {
        "user": public_user(store().one("SELECT * FROM auth_users WHERE id=?", (principal.user_id,))),
        "verification_required": False,
        "email_verification_pending": bool(changing_email and address),
    }


@router.post("/verify-email/request")
def request_verification(request: Request, principal: User) -> dict:
    require_recent(request)
    rate(request, "verify_email.request", principal.user_id, 3)
    auth.request_email_verification(principal.user_id)
    return {"queued": True}


@router.get("/security-events")
def own_security_events(principal: User) -> dict:
    return {
        "events": store().query(
            "SELECT action,created_at,actor_user_id FROM auth_audit_events "
            "WHERE subject_user_id=? ORDER BY created_at DESC LIMIT 50",
            (principal.user_id,),
        )
    }


@router.get("/sessions")
def sessions(principal: User) -> dict:
    rows = store().query(
        "SELECT id,created_at,last_seen_at,expires_at,device,native FROM auth_sessions "
        "WHERE user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY last_seen_at DESC",
        (principal.user_id, time.time()),
    )
    return {"sessions": [{**row, "current": row["id"] == principal.session_id} for row in rows]}


@router.delete("/sessions/{session_id}")
def revoke_session(session_id: str, principal: User) -> dict:
    with store().transaction() as conn:
        result = conn.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE id=? AND user_id=?",
            (time.time(), session_id, principal.user_id),
        )
        if not result.rowcount:
            fail(404, "not_found", "Session not found.")
        audit(conn, "session.revoked", principal.user_id, principal.user_id)
    return {"revoked": True}


@router.get("/users")
def users(
    principal: Admin, offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100)
) -> dict:
    rows = store().query(
        "SELECT u.*,(SELECT MAX(s.created_at) FROM auth_sessions s WHERE s.user_id=u.id) "
        "AS last_sign_in FROM auth_users u ORDER BY u.created_at,u.id LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return {
        "users": [{**public_user(row), "last_sign_in": row["last_sign_in"]} for row in rows],
        "total": store().one("SELECT COUNT(*) AS n FROM auth_users")["n"],
    }


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserChange, request: Request, principal: Admin) -> dict:
    require_recent(request)
    with store().transaction() as conn:
        row = conn.execute("SELECT * FROM auth_users WHERE id=?", (user_id,)).fetchone()
        if not row:
            fail(404, "not_found", "User not found.")
        role, status = body.role or row["role"], body.status or row["status"]
        if row["role"] == "admin" and row["status"] == "active" and (role != "admin" or status != "active"):
            count = conn.execute(
                "SELECT COUNT(*) FROM auth_users WHERE role='admin' AND status='active'"
            ).fetchone()[0]
            if count <= 1:
                fail(409, "last_admin", "The last active administrator cannot be suspended or demoted.")
        conn.execute(
            "UPDATE auth_users SET role=?,status=?,updated_at=? WHERE id=?",
            (role, status, time.time(), user_id),
        )
        conn.execute("UPDATE auth_memberships SET role=? WHERE user_id=?", (role, user_id))
        if role != row["role"] or status != row["status"]:
            auth.revoke(conn, user_id)
            if status == "suspended":
                conn.execute(
                    "UPDATE auth_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (time.time(), user_id),
                )
        if status == "suspended" or (row["role"] == "admin" and role != "admin"):
            conn.execute("INSERT OR REPLACE INTO access_revocations VALUES(?,?)", (user_id, time.time()))
            # Reinstating an account must not restart previously opted-in model calls.
            conn.execute(
                "UPDATE agent_dreams SET environment=NULL, at=NULL, timezone=NULL, enabled_at=NULL, "
                "revision=revision+1, schedule_revision=schedule_revision+1 WHERE at IS NOT NULL "
                "AND agent_id IN (SELECT resource_id FROM access_resources "
                "WHERE kind='agent' AND owner_user_id=?)",
                (user_id,),
            )
        audit(conn, "user.access_changed", principal.user_id, user_id, f"role={role},status={status}")
    pending = False
    if status == "suspended" or (row["role"] == "admin" and role != "admin"):
        from ..access import suspend_owned_work

        try:
            async with asyncio.timeout(5):
                pending = not await suspend_owned_work(user_id)
        except Exception:  # noqa: BLE001 - access was revoked; durable automation cleanup must retry.
            pending = True
    return {
        "user": public_user(store().one("SELECT * FROM auth_users WHERE id=?", (user_id,))),
        "automation_pause_pending": pending,
    }


@router.post("/users/{user_id}/recovery")
def recovery_code(user_id: str, request: Request, principal: Admin) -> dict:
    require_recent(request)
    rate(request, "recovery.admin", principal.user_id, 10)
    with store().transaction() as conn:
        user = conn.execute("SELECT * FROM auth_users WHERE id=? AND status='active'", (user_id,)).fetchone()
        if not user or not user["password_hash"]:
            fail(404, "not_found", "No active password account was found.")
        conn.execute(
            "UPDATE auth_tokens SET revoked_at=? WHERE user_id=? AND purpose='reset'", (time.time(), user_id)
        )
        token_id, code = issue_token(
            conn, "reset", user_id=user_id, address=user["email"], creator=principal.user_id
        )
        expires_at = conn.execute("SELECT expires_at FROM auth_tokens WHERE id=?", (token_id,)).fetchone()[0]
        audit(conn, "password.recovery_code_issued", principal.user_id, user_id)
        if user["email_verified"]:
            auth_mail.notify(
                conn,
                user["email"],
                "A Denkzeug administrator issued a recovery code",
                "An administrator created a one-time password reset code for your account. "
                "It expires in 30 minutes. Contact them if you did not request this.",
            )
    from ..config import settings

    return {
        "code": code,
        "link": auth.link("reset-password", code) if settings.public_url else None,
        "expires_at": expires_at,
    }


@router.get("/invitations")
def invitations(principal: Admin, offset: int = Query(default=0, ge=0)) -> dict:
    return {
        "invitations": [
            _invitation(row)
            for row in store().query(
                "SELECT * FROM auth_tokens WHERE purpose='invite' "
                "ORDER BY created_at DESC LIMIT 100 OFFSET ?",
                (offset,),
            )
        ]
    }


@router.post("/invitations")
def create_invitation(body: Invitation, request: Request, principal: Admin) -> dict:
    require_recent(request)
    rate(request, "invitation.create", principal.user_id, 30)
    address = email(body.email) if body.email else None
    if body.send_email and not address:
        fail(
            422, "email_required_for_delivery", "Enter an optional recipient email, or copy the code instead."
        )
    if body.send_email and not auth_mail.available():
        fail(503, "mail_unavailable", "Configure transactional account mail before sending invitations.")
    with store().transaction() as conn:
        if (
            conn.execute("SELECT registration_mode FROM auth_policy WHERE id=1").fetchone()[0]
            != "invite_only"
        ):
            fail(403, "registration_paused", "Enable invite-only registration before creating an invitation.")
        # Reissuing an invitation invalidates old links/codes for the same address.
        conn.execute(
            "UPDATE auth_tokens SET revoked_at=? WHERE email=? AND purpose='invite' "
            "AND consumed_at IS NULL AND revoked_at IS NULL",
            (time.time(), address),
        )
        token_id, code = issue_token(
            conn,
            "invite",
            address=address,
            role=body.role,
            creator=principal.user_id,
            ttl=body.expires_in_days * 86400,
        )
        from ..config import settings

        conn.execute("UPDATE auth_tokens SET label=? WHERE id=?", (body.label.strip(), token_id))
        url = auth.link("invite", code) if settings.public_url else None
        if body.send_email:
            auth_mail.enqueue(
                conn,
                address,
                "You're invited to Denkzeug",
                "You've been invited to a Denkzeug workspace. Create your account:\n\n"
                + url
                + f"\n\nThis invitation expires in {body.expires_in_days} days.",
                ttl=body.expires_in_days * 86400,
            )
        audit(conn, "invitation.created", principal.user_id, detail=f"role={body.role}")
        row = conn.execute("SELECT * FROM auth_tokens WHERE id=?", (token_id,)).fetchone()
        return {"invitation": _invitation(row), "code": code, "link": url}


@router.post("/invitations/{invitation_id}/revoke")
def revoke_invitation(invitation_id: str, request: Request, principal: Admin) -> dict:
    require_recent(request)
    with store().transaction() as conn:
        changed = conn.execute(
            "UPDATE auth_tokens SET revoked_at=? WHERE id=? AND purpose='invite'",
            (time.time(), invitation_id),
        )
        if not changed.rowcount:
            fail(404, "not_found", "Invitation not found.")
        audit(conn, "invitation.revoked", principal.user_id)
    return {"revoked": True}


@router.get("/policy")
def policy(principal: Admin) -> dict:
    return {
        **store().one("SELECT registration_mode FROM auth_policy WHERE id=1"),
        "mail_available": auth_mail.available(),
    }


@router.patch("/policy")
def update_policy(body: Policy, request: Request, principal: Admin) -> dict:
    require_recent(request)
    with store().transaction() as conn:
        conn.execute("UPDATE auth_policy SET registration_mode=? WHERE id=1", (body.registration_mode,))
        audit(conn, "registration.policy_changed", principal.user_id, detail=body.registration_mode)
    return {"registration_mode": body.registration_mode, "mail_available": auth_mail.available()}


@router.get("/audit")
def audit_events(principal: Admin, offset: int = Query(default=0, ge=0)) -> dict:
    return {
        "events": store().query(
            "SELECT * FROM auth_audit_events ORDER BY created_at DESC LIMIT 100 OFFSET ?", (offset,)
        )
    }


@router.get("/mail-status")
def mail_status(principal: Admin) -> dict:
    return {
        "available": auth_mail.available(),
        "deliveries": store().query(
            "SELECT id,recipient,state,attempts,created_at,expires_at,error FROM auth_mail_outbox "
            "ORDER BY created_at DESC LIMIT 100"
        ),
    }


@router.post("/test-email")
def test_email(request: Request, principal: Admin) -> dict:
    require_recent(request)
    rate(request, "mail.test", principal.user_id, 3)
    if not auth_mail.available():
        fail(503, "mail_unavailable", "Transactional account mail is not configured.")
    address = store().one("SELECT email FROM auth_users WHERE id=?", (principal.user_id,))["email"]
    with store().transaction() as conn:
        auth_mail.enqueue(
            conn, address, "Denkzeug account mail test", "Your account mail delivery is working."
        )
        audit(conn, "mail.test_queued", principal.user_id)
    return {"queued": True}
