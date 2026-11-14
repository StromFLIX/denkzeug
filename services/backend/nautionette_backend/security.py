"""Human sessions and service identity are separate, fail-closed trust boundaries."""

from __future__ import annotations

import hmac
import time
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from .auth_store import digest, fail, store
from .config import settings

SESSION_COOKIE = "dz_session"
CSRF_COOKIE = "dz_csrf"


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str
    session_id: str | None = None
    service: bool = False


current_principal: ContextVar[Principal | None] = ContextVar("account_principal", default=None)


def token_matches(supplied: str | None, expected: str) -> bool:
    return bool(supplied and expected) and hmac.compare_digest(supplied or "", expected)


def validate_public_origin(request: Request) -> None:
    """Credentialless native account flows may use the explicit native CORS origins."""
    if request.headers.get("origin") in settings.cors_origins and not request.cookies.get(SESSION_COOKIE):
        return
    validate_origin(request)


def validate_origin(request: Request) -> None:
    """Browsers send Origin on unsafe requests. Never trust a forwarded Host for links."""
    origin = request.headers.get("origin")
    if not origin:
        return  # Non-browser clients still require a session + CSRF or native bearer.
    canonical = settings.public_url or str(request.base_url).rstrip("/")
    if origin.rstrip("/") != canonical:
        fail(403, "invalid_origin", "This request did not originate from this instance.")


def session_for_token(token: str, *, native: bool | None = None) -> dict:
    if len(token) > 512:
        fail(401, "unauthorized", "Please sign in.")
    row = store().one(
        "SELECT s.*,u.role,u.status,u.email_verified,u.must_change_password FROM auth_sessions s "
        "JOIN auth_users u ON u.id=s.user_id WHERE s.token_hash=?",
        (digest(token),),
    )
    now = time.time()
    if (
        not row
        or row["revoked_at"] is not None
        or row["status"] != "active"
        or row["expires_at"] <= now
        or row["last_seen_at"] + 86400 <= now
        or row["access_expires_at"] <= now
        or (native is not None and bool(row["native"]) != native)
    ):
        fail(401, "unauthorized", "Your session has expired. Please sign in.")
    if row["last_seen_at"] < now - 60:
        store().execute(
            "UPDATE auth_sessions SET last_seen_at=? WHERE id=? AND revoked_at IS NULL", (now, row["id"])
        )
    return row


async def require_user(request: Request) -> Principal:
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if (
        bearer
        and token_matches(bearer, settings.internal_token)
        and token_matches(request.headers.get("x-internal-token"), settings.internal_token)
    ):
        # Service transport does not confer human authority. Only a live,
        # unforgeable turn capability may act as that turn's owning account.
        from .access import actor_for_grant, authorize_request

        actor = request.headers.get("x-denkzeug-actor", "")
        principal = actor_for_grant(actor) if actor else Principal("system", "service", service=True)
        if request.url.path.startswith("/api/auth/"):
            fail(403, "human_required", "Account management requires human authentication.")
        request.state.principal = principal
        current_principal.set(principal)
        await authorize_request(request, principal)
        return principal
    if bearer:
        session = session_for_token(bearer, native=True)
    elif cookie:
        session = session_for_token(cookie, native=False)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            validate_origin(request)
            csrf = request.headers.get("x-csrf-token", "")
            if not token_matches(digest(csrf), session["csrf_hash"]):
                fail(403, "csrf_failed", "Refresh this page before trying again.")
    else:
        fail(401, "unauthorized", "Please sign in.")
    principal = Principal(session["user_id"], session["role"], session["id"])
    request.state.principal = principal
    request.state.auth_session = session
    current_principal.set(principal)
    if session["must_change_password"] and request.url.path not in {
        "/api/auth/me",
        "/api/auth/change-password",
        "/api/auth/logout",
        "/api/auth/reauth",
    }:
        fail(403, "password_change_required", "Choose a new password before continuing.")
    # This guard also applies to direct URLs and agent/API operations, not only navigation.
    if not request.url.path.startswith("/api/auth/"):
        from .access import authorize_request

        await authorize_request(request, principal)
    return principal


async def require_admin(request: Request) -> Principal:
    principal = await require_user(request)
    if principal.role != "admin" or principal.service:
        fail(403, "admin_required", "Administrator access is required.")
    return principal


def require_recent(request: Request) -> None:
    if request.state.auth_session["reauth_at"] < time.time() - 600:
        fail(403, "reauthentication_required", "Confirm your password to continue.")


async def require_internal(request: Request, x_internal_token: str | None = Header(default=None)) -> None:
    if not token_matches(x_internal_token, settings.internal_token):
        raise HTTPException(status_code=401, detail="unauthorized")
    principal = Principal("system", "service", service=True)
    request.state.principal = principal
    current_principal.set(principal)


class AccountBoundaryMiddleware:
    """Reset principal context even for failures/stream termination; protect API docs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from .access import execution_scope

        reset = current_principal.set(None)
        execution_reset = execution_scope.set(None)
        try:
            request = Request(scope)
            if request.url.path.startswith("/api/auth/") and request.method in {"POST", "PUT", "PATCH"}:
                # Bound credential-bearing JSON before FastAPI parses it, including
                # chunked uploads without Content-Length. Other binary APIs retain
                # their own larger, purpose-specific admission budgets.
                chunks, size = [], 0
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > 65536:
                        from starlette.responses import JSONResponse

                        return await JSONResponse(
                            {"detail": "Account request is too large"},
                            status_code=413,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                        )(scope, receive, send)
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                original_receive, body, delivered = receive, b"".join(chunks), False

                async def credential_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await original_receive()

                receive = credential_receive
            if request.url.path in {"/api/docs", "/api/docs/oauth2-redirect", "/api/openapi.json"}:
                try:
                    await require_admin(request)
                except HTTPException as exc:
                    from starlette.responses import JSONResponse

                    return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                        scope, receive, send
                    )

            async def safe_send(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    existing = {key.lower() for key, _ in headers}
                    for key, value in (
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-content-type-options", b"nosniff"),
                    ):
                        if key not in existing:
                            headers.append((key, value))
                    who = getattr(request.state, "principal", None)
                    if who is not None and not who.service:
                        headers.append((b"x-denkzeug-account", who.user_id.encode("ascii")))
                    if request.url.path.startswith("/api/auth/"):
                        headers.extend([(b"cache-control", b"no-store"), (b"pragma", b"no-cache")])
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, safe_send)
        finally:
            current_principal.reset(reset)
            execution_scope.reset(execution_reset)


def secure_cookies() -> bool:
    if not settings.auth_allow_insecure_localhost:
        return True
    return urlsplit(settings.public_url).hostname not in {"localhost", "127.0.0.1", "::1"}
