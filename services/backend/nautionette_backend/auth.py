"""Account lifecycle, provisioning and session primitives.

No dependency on a specific SSO provider: a password credential is optional and
external identities have their own issuer/subject keys. HTTP concerns live in
routers/auth.py; cryptographic/schema primitives live in auth_store.py.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Request, Response

from . import auth_mail
from .auth_store import (
    PASSWORDS,
    audit,
    digest,
    email,
    fail,
    identifier,
    issue_token,
    password_hash,
    public_user,
    secret,
    store,
    username,
    valid_token,
    verify_password,
)
from .config import settings
from .security import CSRF_COOKIE, SESSION_COOKIE, Principal, secure_cookies

log = logging.getLogger(__name__)


def setup_required() -> bool:
    return not bool(store().one("SELECT id FROM auth_users WHERE role='admin' LIMIT 1"))


def link(kind: str, token: str) -> str:
    if not settings.public_url:
        fail(503, "public_url_required", "Configure DENKZEUG_PUBLIC_URL before creating account links.")
    return f"{settings.public_url}/#{kind}={quote(token, safe='')}"


def add_membership(conn: sqlite3.Connection, user_id: str, role: str) -> None:
    conn.execute("INSERT OR IGNORE INTO auth_workspaces VALUES('default','Denkzeug',?)", (time.time(),))
    conn.execute("INSERT INTO auth_memberships VALUES('default',?,?)", (user_id, role))


def _create_admin(conn: sqlite3.Connection, name: str, address: str, encoded: str, temporary: bool) -> str:
    if conn.execute("SELECT 1 FROM auth_users WHERE role='admin'").fetchone():
        fail(409, "already_initialized", "This instance already has an administrator.")
    user_id, now = identifier(), time.time()
    conn.execute(
        "INSERT INTO auth_users(id,username,email,display_name,role,email_verified,must_change_password,"
        "password_hash,created_at,updated_at) VALUES(?,?,?,?,'admin',1,?,?,?,?)",
        (user_id, name, address, name, int(temporary), encoded, now, now),
    )
    add_membership(conn, user_id, "admin")
    # Account and legacy ownership are one transaction. Nothing is assigned to a
    # later invitee, and a crash cannot leave a claimed instance with unowned data.
    from .access import claim_legacy

    claim_legacy(conn, user_id)
    conn.execute("UPDATE auth_tokens SET revoked_at=? WHERE purpose='setup' AND revoked_at IS NULL", (now,))
    audit(conn, "bootstrap.completed", user_id, user_id)
    return user_id


def issue_setup_token() -> str:
    if not setup_required():
        fail(409, "already_initialized", "An administrator already exists. Use local recovery instead.")
    with store().transaction() as conn:
        conn.execute(
            "UPDATE auth_tokens SET revoked_at=? WHERE purpose='setup' AND revoked_at IS NULL", (time.time(),)
        )
        _, token = issue_token(conn, "setup")
        audit(conn, "bootstrap.link_issued")
    return token


def initialize() -> None:
    """Idempotent startup; environment credentials are never a recurring reset."""
    if not setup_required():
        if settings.app_token:
            log.warning("APP_TOKEN no longer authenticates users; upgrade clients and use account sign-in")
        return
    supplied = any(
        (
            settings.bootstrap_username,
            settings.bootstrap_email,
            settings.bootstrap_password,
            settings.bootstrap_password_file,
        )
    )
    if supplied:
        password = (
            Path(settings.bootstrap_password_file).read_text().rstrip("\r\n")
            if settings.bootstrap_password_file
            else settings.bootstrap_password
        )
        if not (settings.bootstrap_username and settings.bootstrap_email and password):
            raise ValueError("Supply all bootstrap administrator credentials, or none")
        if settings.bootstrap_password_file and settings.bootstrap_password:
            raise ValueError("Use either bootstrap PASSWORD_FILE or PASSWORD, not both")
        name, address, encoded = (
            username(settings.bootstrap_username),
            email(settings.bootstrap_email),
            password_hash(password),
        )
        with store().transaction() as conn:
            if not conn.execute("SELECT 1 FROM auth_users WHERE role='admin'").fetchone():
                _create_admin(conn, name, address, encoded, temporary=True)
        log.warning("Initial administrator provisioned; sign in and replace the temporary password")
    else:
        # Never publish this secret into the app's event bus, health, tracing or tool responses.
        if settings.public_url:
            token = issue_setup_token()
            log.warning(
                "Account setup required. One-time administrator setup link (expires in 30 minutes): %s",
                link("bootstrap", token),
            )
        else:
            log.warning(
                "Account setup required. Configure DENKZEUG_PUBLIC_URL, then issue a setup link "
                "with the local account administration command. All user APIs remain locked."
            )


def bootstrap(token: str, name: str, address: str, password: str) -> dict:
    name, address, encoded = username(name), email(address), password_hash(password)
    try:
        with store().transaction() as conn:
            valid_token(conn, token, "setup")
            user_id = _create_admin(conn, name, address, encoded, temporary=False)
    except sqlite3.IntegrityError:
        fail(409, "account_conflict", "That account identity is unavailable.")
    return store().one("SELECT * FROM auth_users WHERE id=?", (user_id,))


def authenticate(name: str, password: str) -> dict:
    row = store().one("SELECT * FROM auth_users WHERE username=? COLLATE NOCASE", (name.strip(),))
    valid = verify_password(row["password_hash"] if row else None, password)
    if not valid or row["status"] != "active" or not row["email_verified"]:
        with store().transaction() as conn:
            audit(conn, "signin.failed")
        fail(
            401,
            "invalid_credentials",
            "The username or password is incorrect, or the account is unavailable.",
        )
    if PASSWORDS.check_needs_rehash(row["password_hash"]):
        store().execute(
            "UPDATE auth_users SET password_hash=? WHERE id=? AND password_hash=?",
            (PASSWORDS.hash(password), row["id"], row["password_hash"]),
        )
    return row


def create_session(user: dict, request: Request, response: Response, native: bool = False) -> dict:
    now, token, csrf = time.time(), secret(), secret()
    refresh = secret() if native else None
    session_id = identifier()
    device = request.headers.get("user-agent", "Unknown device")[:250]
    with store().transaction() as conn:
        # Recheck after password verification; suspension/password changes can race Argon2.
        current = conn.execute(
            "SELECT status,password_hash FROM auth_users WHERE id=?", (user["id"],)
        ).fetchone()
        if not current or current["status"] != "active" or current["password_hash"] != user["password_hash"]:
            fail(401, "account_changed", "Please sign in again.")
        conn.execute(
            "INSERT INTO auth_sessions(id,user_id,token_hash,csrf_hash,refresh_hash,native,created_at,"
            "last_seen_at,expires_at,access_expires_at,reauth_at,device) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                user["id"],
                digest(token),
                digest(csrf),
                digest(refresh) if refresh else None,
                int(native),
                now,
                now,
                now + 30 * 86400,
                now + (600 if native else 30 * 86400),
                now,
                device,
            ),
        )
        audit(conn, "signin.succeeded", user["id"], user["id"])
    result = {"user": public_user(user), "session_id": session_id, "csrf_token": "" if native else csrf}
    if native:
        result.update(access_token=token, refresh_token=refresh, expires_in=600)
    else:
        for name, value, http_only in ((SESSION_COOKIE, token, True), (CSRF_COOKIE, csrf, False)):
            response.set_cookie(
                name,
                value,
                max_age=30 * 86400,
                httponly=http_only,
                secure=secure_cookies(),
                samesite="lax",
                path="/",
            )
    return result


def rotate_refresh(token: str) -> dict:
    hashed, now = digest(token), time.time()
    replay = False
    with store().transaction() as conn:
        row = conn.execute(
            "SELECT s.*,u.status,u.email_verified FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id "
            "WHERE s.refresh_hash=? AND s.native=1",
            (hashed,),
        ).fetchone()
        if not row:
            previous = conn.execute(
                "SELECT session_id FROM auth_consumed_refresh WHERE token_hash=?", (hashed,)
            ).fetchone()
            if previous:
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at=? WHERE id=?", (now, previous["session_id"])
                )
                audit(conn, "session.refresh_replay")
                replay = True
        elif (
            row["revoked_at"] is None
            and row["expires_at"] > now
            and row["last_seen_at"] + 86400 > now
            and row["status"] == "active"
            and row["email_verified"]
        ):
            access, refresh = secret(), secret()
            conn.execute(
                "INSERT INTO auth_consumed_refresh VALUES(?,?,?)", (hashed, row["id"], row["expires_at"])
            )
            conn.execute(
                "UPDATE auth_sessions SET token_hash=?,refresh_hash=?,access_expires_at=?,"
                "last_seen_at=? WHERE id=?",
                (digest(access), digest(refresh), now + 600, now, row["id"]),
            )
            return {"access_token": access, "refresh_token": refresh, "expires_in": 600}
    # Raising outside the transaction is essential: otherwise replay revocation rolls back.
    fail(401, "refresh_reused" if replay else "unauthorized", "Please sign in again.")


def revoke(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute(
        "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (time.time(), user_id)
    )


def invitation(code: str) -> dict:
    with store().transaction() as conn:
        policy = conn.execute("SELECT registration_mode FROM auth_policy WHERE id=1").fetchone()
        if policy["registration_mode"] != "invite_only":
            fail(403, "registration_paused", "Registration is paused. Contact your administrator.")
        row = valid_token(conn, code, "invite")
        return {key: row[key] for key in ("email", "role", "expires_at")}


def register(code: str, name: str, password: str) -> dict:
    if not auth_mail.available():
        fail(503, "mail_unavailable", "Account email is unavailable. Contact your administrator.")
    name, encoded = username(name), password_hash(password)
    try:
        with store().transaction() as conn:
            policy = conn.execute("SELECT registration_mode FROM auth_policy WHERE id=1").fetchone()
            if policy["registration_mode"] != "invite_only":
                fail(403, "registration_paused", "Registration is paused. Contact your administrator.")
            invite = valid_token(conn, code, "invite")
            user_id, now = identifier(), time.time()
            conn.execute(
                "INSERT INTO auth_users(id,username,email,display_name,role,status,password_hash,created_at,"
                "updated_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
                (user_id, name, invite["email"], name, invite["role"], encoded, now, now),
            )
            add_membership(conn, user_id, invite["role"])
            conn.execute("UPDATE auth_tokens SET consumed_at=? WHERE id=?", (now, invite["id"]))
            _, token = issue_token(conn, "verify_email", user_id=user_id, address=invite["email"], ttl=86400)
            auth_mail.enqueue(
                conn,
                invite["email"],
                "Verify your Denkzeug email",
                "Verify your email address to activate your account:\n\n"
                + link("verify-email", token)
                + "\n\nThis link expires in 24 hours.",
                ttl=86400,
            )
            audit(conn, "account.registered", user_id, user_id)
    except sqlite3.IntegrityError:
        fail(
            409, "account_conflict", "The username or email is unavailable. Ask your administrator for help."
        )
    return {"verification_required": True}


def verify_email(token: str) -> None:
    with store().transaction() as conn:
        record = conn.execute(
            "SELECT purpose FROM auth_tokens WHERE token_hash=?", (digest(token),)
        ).fetchone()
        purpose = record["purpose"] if record else "verify_email"
        if purpose not in {"verify_email", "change_email"}:
            fail(400, "invalid_link", "This verification link is invalid.")
        row = valid_token(conn, token, purpose)
        user = conn.execute("SELECT * FROM auth_users WHERE id=?", (row["user_id"],)).fetchone()
        if not user or user["status"] == "suspended":
            fail(400, "invalid_link", "This verification link is unavailable.")
        now = time.time()
        try:
            if purpose == "verify_email":
                conn.execute(
                    "UPDATE auth_users SET email_verified=1,status='active',updated_at=? WHERE id=?",
                    (now, user["id"]),
                )
            else:
                conn.execute(
                    "UPDATE auth_users SET email=?,email_verified=1,updated_at=? WHERE id=?",
                    (row["email"], now, user["id"]),
                )
                revoke(conn, user["id"])
            conn.execute("UPDATE auth_tokens SET consumed_at=? WHERE id=?", (now, row["id"]))
            audit(conn, "account.email_verified", user["id"], user["id"])
        except sqlite3.IntegrityError:
            fail(409, "account_conflict", "This email address is no longer available.")


def forgot_password(address: str) -> None:
    if not auth_mail.available():
        fail(503, "mail_unavailable", "Account email is unavailable. Contact your administrator.")
    with store().transaction() as conn:
        row = conn.execute("SELECT * FROM auth_users WHERE email=? COLLATE NOCASE", (address,)).fetchone()
        if not row or row["status"] == "suspended":
            return
        if row["status"] == "pending":
            purpose, title, action = "verify_email", "Verify your Denkzeug email", "Verify your email"
        elif not row["password_hash"]:
            auth_mail.enqueue(
                conn,
                row["email"],
                "Your Denkzeug sign-in method",
                "Your account uses single sign-on. Use your configured identity provider to sign in.",
            )
            return
        else:
            purpose, title, action = "reset", "Reset your Denkzeug password", "Choose a new password"
        conn.execute(
            "UPDATE auth_tokens SET revoked_at=? WHERE user_id=? AND purpose=? AND revoked_at IS NULL",
            (time.time(), row["id"], purpose),
        )
        _, token = issue_token(conn, purpose, user_id=row["id"], address=row["email"])
        route = "verify-email" if purpose == "verify_email" else "reset-password"
        auth_mail.enqueue(
            conn,
            row["email"],
            title,
            action
            + ":\n\n"
            + link(route, token)
            + "\n\nThis link expires in 30 minutes. If you did not request it, ignore this email.",
        )
        audit(conn, "recovery.requested", subject=row["id"])


def reset_password(token: str, password: str) -> None:
    encoded, now = password_hash(password), time.time()
    with store().transaction() as conn:
        record = valid_token(conn, token, "reset")
        user = conn.execute("SELECT * FROM auth_users WHERE id=?", (record["user_id"],)).fetchone()
        if not user or user["status"] != "active" or not user["password_hash"]:
            fail(400, "invalid_link", "This recovery link is unavailable.")
        conn.execute(
            "UPDATE auth_users SET password_hash=?,must_change_password=0,updated_at=? WHERE id=?",
            (encoded, now, user["id"]),
        )
        conn.execute(
            "UPDATE auth_tokens SET revoked_at=? WHERE user_id=? AND purpose IN ('reset','change_email')",
            (now, user["id"]),
        )
        conn.execute("UPDATE auth_tokens SET consumed_at=? WHERE id=?", (now, record["id"]))
        revoke(conn, user["id"])
        if auth_mail.available():
            auth_mail.enqueue(
                conn,
                user["email"],
                "Your Denkzeug password changed",
                "Your password was reset and all devices were signed out. "
                "If this was not you, contact your instance administrator immediately.",
            )
        audit(conn, "password.reset", user["id"], user["id"])


def change_password(principal: Principal, current: str, password: str) -> None:
    row = store().one("SELECT * FROM auth_users WHERE id=?", (principal.user_id,))
    if not verify_password(row["password_hash"], current):
        fail(403, "invalid_credentials", "The current password is incorrect.")
    if verify_password(row["password_hash"], password):
        fail(422, "password_unchanged", "Choose a different password.")
    encoded, now = password_hash(password), time.time()
    with store().transaction() as conn:
        result = conn.execute(
            "UPDATE auth_users SET password_hash=?,must_change_password=0,updated_at=? "
            "WHERE id=? AND password_hash=?",
            (encoded, now, row["id"], row["password_hash"]),
        )
        if result.rowcount != 1:
            fail(409, "account_changed", "The account changed. Please sign in again.")
        revoke(conn, row["id"])
        conn.execute(
            "UPDATE auth_tokens SET revoked_at=? WHERE user_id=? AND purpose IN ('reset','change_email')",
            (now, row["id"]),
        )
        if auth_mail.available():
            auth_mail.enqueue(
                conn,
                row["email"],
                "Your Denkzeug password changed",
                "Your password changed and all devices were signed out. "
                "If this was not you, contact your administrator immediately.",
            )
        audit(conn, "password.changed", row["id"], row["id"])


def recover_password(name: str, password: str) -> None:
    """Local operator only. Never exposed as an HTTP or agent tool operation."""
    encoded, now = password_hash(password), time.time()
    with store().transaction() as conn:
        user = conn.execute("SELECT * FROM auth_users WHERE username=? COLLATE NOCASE", (name,)).fetchone()
        if not user:
            fail(404, "not_found", "No such account.")
        conn.execute(
            "UPDATE auth_users SET password_hash=?,must_change_password=1,updated_at=? WHERE id=?",
            (encoded, now, user["id"]),
        )
        revoke(conn, user["id"])
        conn.execute("UPDATE auth_tokens SET revoked_at=? WHERE user_id=?", (now, user["id"]))
        audit(conn, "operator.password_recovery", subject=user["id"])
