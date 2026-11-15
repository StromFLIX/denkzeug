"""Read-only staging check: matching backend, authenticated helper and desktop API.

No CLI is launched, license accepted or OAuth/account data accessed. Credentials
come only from CI environment and neither responses nor exception text are logged.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from scripts.account_session import AccountSession
except ModuleNotFoundError:
    from account_session import AccountSession


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def check(base: str, token: str = "") -> dict:
    opener = build_opener(NoRedirect())
    session = (
        AccountSession(base, token=token, opener=opener)
        if token
        else AccountSession.from_env(base, opener=opener)
    )
    with session:
        return _check(base, session)


def _check(base: str, session: AccountSession) -> dict:
    try:
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
        ):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "Configure HTTPS APP_URL and the dedicated QA account for the CLI OAuth staging check"
        ) from exc
    request = Request(  # noqa: S310 - validated configured CI application origin
        base.rstrip("/") + "/api/mcp-oauth/capabilities"
    )
    try:
        with session.open(request, timeout=30) as response:  # noqa: S310
            body = response.read(65537)
        if len(body) > 65536:
            raise ValueError
        value = json.loads(body)
        if (
            value.get("available") is not True
            or value.get("desktop_bridge_version") != 1
            or value.get("http", {}).get("available") is not True
        ):
            raise ValueError
        hosted = value.get("http", {})
        if not {"authorization_code", "device_code", "client_credentials"}.issubset(
            hosted.get("grant_types", [])
        ) or not {
            "none",
            "client_secret_basic",
            "client_secret_post",
            "private_key_jwt",
            "client_secret_jwt",
        }.issubset(hosted.get("token_endpoint_auth_methods", [])):
            raise ValueError
        callback = hosted.get("web_callback")
        if (
            not isinstance(callback, str)
            or not callback.startswith("https://")
            or not callback.endswith("/api/mcp-oauth/web-callback")
        ):
            raise ValueError
        metadata_request = Request(  # noqa: S310 - same validated application origin, never a callback mutation
            base.rstrip("/") + "/api/mcp-oauth/client-metadata"
        )
        with session.open(metadata_request, timeout=30) as response:  # noqa: S310
            raw_metadata = response.read(65537)
        if len(raw_metadata) > 65536:
            raise ValueError
        metadata = json.loads(raw_metadata)
        if (
            metadata.get("client_id") != callback.rsplit("/", 1)[0] + "/client-metadata"
            or metadata.get("redirect_uris") != [callback]
            or metadata.get("token_endpoint_auth_method") != "none"
        ):
            raise ValueError
        discovery = Request(  # noqa: S310 - same validated application origin, read-only metadata
            base.rstrip("/") + "/api/mcp-oauth/discover",
            method="POST",
            data=json.dumps({"url": "https://workiq.svc.cloud.microsoft/mcp"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with session.open(discovery, timeout=60) as response:  # noqa: S310
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError
        profile = json.loads(raw)
        if (
            profile.get("url") != "https://workiq.svc.cloud.microsoft/mcp"
            or profile.get("client_id") != "ba081686-5d24-4bc6-a0d6-d034ecffed87"
            or profile.get("redirect_uri") != "http://localhost:12798"
        ):
            raise ValueError
    except (HTTPError, URLError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            "CLI OAuth staging check failed: deploy the matching authenticated gateway/backend images"
        ) from exc
    return {
        "available": True,
        "desktop_bridge_version": 1,
        "https_oauth": True,
        "workiq_discovery": True,
        "generic_oauth_grants": True,
        "confidential_clients": True,
        "web_callback_metadata": True,
    }


if __name__ == "__main__":
    try:
        result = check(os.environ.get("APP_URL", ""))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1) from None
    print("MCP OAuth staging compatibility: " + json.dumps(result))
