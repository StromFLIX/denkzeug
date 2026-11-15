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
    token_id: str | None = None


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
    existing = getattr(request.state, "principal", None)
    if existing is not None:
        return existing
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    cookie = request.cookies.get(SESSION_COOKIE, "")
    from . import api_keys
    from .api_policy import resolve

    policy = getattr(request.state, "api_policy", None)
    if policy is None:
        from .main import app

        policy = resolve(request, app)
    query_token = request.query_params.get("token")
    if query_token and getattr(request.state, "api_route", "") == "/api/triggers/{name}":
        if bearer:
            fail(400, "ambiguous_credentials", "Supply one credential, not both a URL token and bearer.")
        bearer = query_token
    if bearer.startswith(("dz_key_", "dz_wht_")):
        row = api_keys.identify(bearer, request)
        api_keys.authorize_key(request, row, policy)
        principal = Principal(row["owner_user_id"], row["role"], token_id=row["id"])
        request.state.principal = principal
        current_principal.set(principal)
        from .access import authorize_request

        await authorize_request(request, principal)
        if row["kind"] == "webhook":
            api_keys.webhook_limit(row)
        return principal
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
    if not getattr(request.state, "auth_session", None):
        fail(403, "human_required", "Confirm this operation using a signed-in user session.")
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

    async def reject(self, request: Request, status: int, detail, receive, send, extra_headers=None):
        """Admission errors still need native-client CORS and safe response headers."""
        from starlette.datastructures import Headers
        from starlette.middleware.cors import CORSMiddleware
        from starlette.responses import JSONResponse

        headers = {
            **(extra_headers or {}),
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
        who = getattr(request.state, "principal", None)
        if who is not None and not who.service:
            headers["X-Denkzeug-Account"] = who.user_id
        response = JSONResponse({"detail": detail}, status_code=status, headers=headers)
        request_headers = Headers(scope=request.scope)
        if not request_headers.get("origin"):
            return await response(request.scope, receive, send)
        cors = CORSMiddleware(
            response,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Denkzeug-Account"],
        )
        # Do not re-run preflight admission: preserve an invalid preflight's status.
        return await cors.simple_response(request.scope, receive, send, request_headers=request_headers)

    async def websocket(self, scope, receive, send):
        from starlette.requests import HTTPConnection

        from . import api_keys
        from .account_streams import live
        from .api_policy import resolve
        from .main import app as root_app

        try:
            policy = resolve(HTTPConnection(scope), root_app)
        except HTTPException:
            return await send({"type": "websocket.close", "code": 1008})
        if policy.authentication == "ticket":
            # The reviewed speech transport validates a bounded, single-use
            # session-bound ticket before accepting a connection.
            return await self.app(scope, receive, send)
        if policy.authentication not in {"account", "session"}:
            return await send({"type": "websocket.close", "code": 1008})
        from .access import execution_scope

        reset = current_principal.set(None)
        execution_reset = execution_scope.set(None)
        request = Request({**scope, "type": "http", "method": "GET", "state": scope.setdefault("state", {})})
        audit_id = None
        started = time.monotonic()
        try:
            if request.cookies.get(SESSION_COOKIE):
                validate_origin(request)
            who = await require_user(request)
            if policy.authentication == "session" and not getattr(request.state, "auth_session", None):
                raise HTTPException(403, "A signed-in user is required")
            audit_id = api_keys.begin_request(request)

            async def guarded_receive():
                if not live(who):
                    return {"type": "websocket.disconnect", "code": 1008}
                message = await receive()
                if not live(who):
                    return {"type": "websocket.disconnect", "code": 1008}
                return message

            async def guarded_send(message):
                if message["type"] in {"websocket.accept", "websocket.send"} and not live(who):
                    return await send({"type": "websocket.close", "code": 1008})
                await send(message)

            await self.app(scope, guarded_receive, guarded_send)
            api_keys.finish_request(audit_id, 200, time.monotonic() - started, request)
        except HTTPException as exc:
            audit_id = audit_id or api_keys.begin_request(request)
            api_keys.finish_request(audit_id, exc.status_code, time.monotonic() - started, request)
            await send({"type": "websocket.close", "code": 1008})
        finally:
            current_principal.reset(reset)
            execution_scope.reset(execution_reset)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await self.websocket(scope, receive, send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from .access import execution_scope

        reset = current_principal.set(None)
        execution_reset = execution_scope.set(None)
        from . import api_keys
        from .api_policy import admit

        transport_reset = api_keys.transport_secret.set(None)
        audit_id = None
        started = time.monotonic()
        response_started = False
        try:
            request = Request(scope)
            authorization = request.headers.get("authorization", "")
            raw = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            if not raw and request.url.path.startswith("/api/triggers/"):
                raw = request.query_params.get("token", "")
            if raw.startswith(("dz_key_", "dz_wht_")):
                api_keys.observe(raw, request)
            if (
                request.method == "OPTIONS"
                and request.headers.get("origin")
                and request.headers.get("access-control-request-method")
            ):
                # A preflight is not an API invocation; CORS may answer only for
                # an explicitly registered operation, never an unreviewed URL.
                from .api_policy import resolve
                from .main import app as root_app

                probe_scope = {
                    **scope,
                    "method": request.headers["access-control-request-method"],
                    "state": {},
                }
                try:
                    resolve(Request(probe_scope), root_app)
                except HTTPException as exc:
                    return await self.reject(request, exc.status_code, exc.detail, receive, send, exc.headers)
                return await self.app(scope, receive, send)
            credential_json = request.url.path.startswith(
                ("/api/auth/", "/api/tokens")
            ) or request.url.path.endswith("/webhook-token")
            if credential_json and request.method in {"POST", "PUT", "PATCH"}:
                maximum = 65536 if request.url.path.startswith("/api/auth/") else 1048576
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
                    if size > maximum:
                        audit_id = api_keys.begin_request(request)
                        api_keys.finish_request(audit_id, 413, time.monotonic() - started, request)
                        return await self.reject(
                            request, 413, "Credential request is too large", receive, send
                        )
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
            request = Request(scope, receive)
            try:
                # Attribute even denied/expired/unknown-route attempts to known keys.
                authorization = request.headers.get("authorization", "")
                raw = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
                if not raw and request.url.path.startswith("/api/triggers/"):
                    raw = request.query_params.get("token", "")
                from .api_policy import resolve
                from .main import app as root_app

                resolve(request, root_app)
                if raw.startswith(("dz_key_", "dz_wht_")):
                    api_keys.identify(raw, request)
                await admit(request, root_app)
                if request.url.path in {"/api/docs", "/api/docs/oauth2-redirect", "/api/openapi.json"}:
                    await require_admin(request)
            except HTTPException as exc:
                audit_id = api_keys.begin_request(request)
                api_keys.finish_request(audit_id, exc.status_code, time.monotonic() - started, request)
                return await self.reject(request, exc.status_code, exc.detail, receive, send, exc.headers)
            audit_id = api_keys.begin_request(request)
            # Ownership guards may have parsed JSON. Replay its cached body to the
            # router's Request, without ever losing bytes or re-consuming a stream.
            if hasattr(request, "_body"):
                buffered, delivered, original_receive = request._body, False, receive

                async def replay_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": buffered, "more_body": False}
                    return await original_receive()

                receive = replay_receive

            async def safe_send(message):
                nonlocal response_started
                if message["type"] == "http.response.start":
                    response_started = True
                    api_keys.finish_request(audit_id, message["status"], time.monotonic() - started, request)
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
                    if request.url.path.startswith(
                        ("/api/auth/", "/api/tokens", "/api/triggers/")
                    ) or request.url.path.endswith("/webhook-token"):
                        headers.extend([(b"cache-control", b"no-store"), (b"pragma", b"no-cache")])
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, safe_send)
        finally:
            if audit_id is not None and not response_started:
                # Denials were finalized above; crashes before response are distinct.
                row = api_keys.store().one("SELECT outcome FROM api_key_requests WHERE id=?", (audit_id,))
                if row and row["outcome"] == "started":
                    api_keys.finish_request(audit_id, 500, time.monotonic() - started, request)
            api_keys.transport_secret.reset(transport_reset)
            current_principal.reset(reset)
            execution_scope.reset(execution_reset)


def secure_cookies() -> bool:
    if not settings.auth_allow_insecure_localhost:
        return True
    return urlsplit(settings.public_url).hostname not in {"localhost", "127.0.0.1", "::1"}
