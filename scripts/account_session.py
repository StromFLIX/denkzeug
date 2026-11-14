"""Per-process native QA sessions; stdlib transport plus an optional httpx adapter.

No tokens are written to files or logs. Authentication and refresh mutations are
never retried after uncertain transport failures. Only a definite API HTTP 401
allows one refresh and one replay (the backend rejects before route execution).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class SessionError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AccountSession:
    def __init__(self, base, *, username="", password="", token="", opener=None):
        try:
            parsed = urlsplit(base)
            port = parsed.port
            origin = (
                parsed.scheme,
                parsed.hostname,
                port if port is not None else (443 if parsed.scheme == "https" else 80),
            )
        except ValueError:
            raise SessionError("Configure a valid canonical app origin") from None
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not (
                parsed.scheme == "https"
                or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        ):
            raise SessionError("Configure a direct HTTPS app origin, or isolated localhost development")
        self.base = base.rstrip("/")
        self._origin = origin
        self._opener = opener or urllib.request.build_opener(NoRedirect())
        self._token, self._refresh = token, ""
        self._deadline = float("inf") if token else 0
        self._manual = bool(token)
        self._closed = False
        if token:
            if not self._valid_credential(token):
                raise SessionError("Manual device access token is malformed")
            if username or password:
                raise SessionError(
                    "Choose account credentials or the explicit manual token override, not both"
                )
        elif username and password:
            result = self._auth("login", {"username": username, "password": password, "native": True})
            self._accept(result)
            user = result.get("user")
            if not isinstance(user, dict) or type(user.get("must_change_password")) is not bool:
                self.close()
                raise SessionError("Account API returned an incomplete user session")
            if user["must_change_password"]:
                self.close()
                raise SessionError("QA account requires a password change; sign in interactively first")
        else:
            raise SessionError("Configure DENKZEUG_SMOKE_USERNAME and DENKZEUG_SMOKE_PASSWORD[_FILE]")

    @classmethod
    def from_env(cls, base, *, opener=None):
        token = os.environ.get("DENKZEUG_DEVICE_TOKEN", "")
        if token:
            if os.environ.get("DENKZEUG_ALLOW_DEVICE_TOKEN", "").lower() != "true":
                raise SessionError("Manual DENKZEUG_DEVICE_TOKEN requires DENKZEUG_ALLOW_DEVICE_TOKEN=true")
            return cls(base, token=token, opener=opener)
        password = os.environ.get("DENKZEUG_SMOKE_PASSWORD", "")
        filename = os.environ.get("DENKZEUG_SMOKE_PASSWORD_FILE", "")
        if filename and password:
            raise SessionError("Configure only one smoke password source")
        if filename:
            try:
                password = Path(filename).read_text().removesuffix("\n").removesuffix("\r")
            except OSError:
                raise SessionError("Cannot read the QA password file") from None
        return cls(
            base, username=os.environ.get("DENKZEUG_SMOKE_USERNAME", ""), password=password, opener=opener
        )

    def _auth(self, action, payload):
        request = urllib.request.Request(  # noqa: S310 - canonical HTTPS/loopback origin validated above
            self.base + "/api/auth/" + action,
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                return json.load(response)
        except (OSError, ValueError, TypeError):
            raise SessionError(
                "Account sign-in/refresh failed; inspect QA account availability. "
                "Never replay an uncertain refresh."
            ) from None

    @staticmethod
    def _valid_credential(value):
        return (
            isinstance(value, str)
            and 0 < len(value) <= 512
            and value.isascii()
            and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
        )

    def _accept(self, result):
        try:
            token, refresh, expiry = result["access_token"], result["refresh_token"], result["expires_in"]
            if not self._valid_credential(token) or not self._valid_credential(refresh):
                raise ValueError
            if type(expiry) is not int or expiry <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise SessionError("Account API returned an invalid session") from None
        self._token, self._refresh = token, refresh
        self._deadline = time.monotonic() + max(0, expiry - min(30, expiry / 2))

    def refresh(self):
        if self._manual:
            raise SessionError("Manual device access token expired or was rejected; sign in again")
        refresh, self._refresh = self._refresh, ""
        if not refresh:
            raise SessionError("Account refresh is unavailable; start a new authenticated run")
        # Consume locally before I/O too: an uncertain response must never replay it.
        self._accept(self._auth("refresh", {"refresh_token": refresh}))

    def authorization(self):
        if self._closed:
            raise SessionError("Account session is closed")
        if time.monotonic() >= self._deadline:
            self.refresh()
        return "Bearer " + self._token

    def _validate_target(self, url):
        try:
            parsed = urlsplit(str(url))
            port = parsed.port
            origin = (
                parsed.scheme,
                parsed.hostname,
                port if port is not None else (443 if parsed.scheme == "https" else 80),
            )
        except ValueError:
            raise SessionError("Refusing an invalid account request origin") from None
        if origin != self._origin or parsed.username or parsed.password:
            raise SessionError("Refusing to send account credentials to another origin")

    def open(self, request, timeout=30):
        self._validate_target(request.full_url)
        request.add_header("Authorization", self.authorization())
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            error.close()
            self.refresh()
            request.add_header("Authorization", self.authorization())
            return self._opener.open(request, timeout=timeout)

    def httpx_auth(self):
        import httpx

        owner = self

        class NativeAuth(httpx.Auth):
            def auth_flow(self, request):
                owner._validate_target(request.url)
                request.headers["Authorization"] = owner.authorization()
                response = yield request
                if response.status_code == 401:
                    response.read()
                    owner.refresh()
                    request.headers["Authorization"] = owner.authorization()
                    yield request

        return NativeAuth()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if not self._manual and self._token:
            request = urllib.request.Request(  # noqa: S310 - canonical HTTPS/loopback origin validated above
                self.base + "/api/auth/logout",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._token},
            )
            try:
                with self._opener.open(request, timeout=15):
                    pass
            except (OSError, ValueError):
                pass  # Best-effort cleanup; never obscure the functional test result.
        self._token = self._refresh = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
