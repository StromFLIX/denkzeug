"""QA helper exercises real public sign-in, refresh and logout without overrides."""

import io
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from nautionette_backend import main

from scripts.account_session import AccountSession


def test_qa_session_recovers_expired_access_and_revokes_at_exit(backend):
    http = TestClient(main.app, base_url="https://testserver")
    calls = []

    class Opener:
        def open(self, request, timeout):
            path = urlsplit(request.full_url).path
            calls.append(path)
            result = http.request(
                request.get_method(), path, content=request.data, headers=dict(request.header_items())
            )
            if result.status_code >= 400:
                raise urllib.error.HTTPError(request.full_url, result.status_code, "HTTP failure", {}, None)
            return io.BytesIO(result.content)

    with AccountSession(
        "https://testserver",
        username="admin",
        password="a correct horse battery staple for tests",
        opener=Opener(),
    ) as account:
        original = account._token
        backend.db.execute("UPDATE auth_sessions SET access_expires_at=0 WHERE id!='test-session'")
        with account.open(urllib.request.Request("https://testserver/api/auth/me")) as response:
            assert b'"username":"admin"' in response.read()
        assert account._token != original
        assert calls == ["/api/auth/login", "/api/auth/me", "/api/auth/refresh", "/api/auth/me"]
        access = account._token
    assert calls[-1] == "/api/auth/logout"
    assert http.get("/api/auth/me", headers={"Authorization": "Bearer " + access}).status_code == 401
    assert account._token == account._refresh == ""
