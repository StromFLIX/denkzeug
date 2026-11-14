"""Exercise the CI login helper against real account/password/session handling."""

import json
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from nautionette_backend import main
from nautionette_backend.auth_store import digest

from scripts import smoke_auth


def test_smoke_helper_uses_real_native_password_change_and_login(backend, monkeypatch, tmp_path):
    http = TestClient(main.app, base_url="https://testserver")
    backend.db.execute("UPDATE auth_users SET must_change_password=1 WHERE username='admin'")
    (tmp_path / "bootstrap-password").write_text("a correct horse battery staple for tests")
    (tmp_path / "account-password").write_text("a replacement private passphrase for smoke 956!")
    initial_tokens = []

    class Response:
        def __init__(self, response):
            assert response.status_code == 200
            self.response = response

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.response.content

    class Opener:
        def open(self, request, timeout):
            response = http.post(
                urlsplit(request.full_url).path,
                content=request.data,
                headers=dict(request.header_items()),
            )
            if json.loads(request.data).get("password") == "a correct horse battery staple for tests":
                initial_tokens.append(response.json()["access_token"])
            return Response(response)

    monkeypatch.setattr(smoke_auth.urllib.request, "build_opener", lambda *args: Opener())
    token = smoke_auth.authenticate("https://testserver", "admin", tmp_path, initialize=True)
    result = http.get("/api/auth/me", headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    assert result.json()["user"]["must_change_password"] is False
    assert len(initial_tokens) == 1
    assert (
        http.get("/api/auth/me", headers={"Authorization": "Bearer " + initial_tokens[0]}).status_code == 401
    )
    assert (
        backend.db.one("SELECT native FROM auth_sessions WHERE token_hash=?", (digest(token),))["native"] == 1
    )
