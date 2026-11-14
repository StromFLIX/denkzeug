"""CI uses normal native login and mandatory password replacement, not a bypass."""

import io
import json
import urllib.error
from pathlib import Path

import pytest

from scripts import smoke_auth


@pytest.fixture
def credentials(tmp_path):
    (tmp_path / "bootstrap-password").write_text("temporary-bootstrap-fixture\n")
    (tmp_path / "account-password").write_text("replacement-password-fixture\n")
    return tmp_path


def test_smoke_bootstrap_replaces_password_then_signs_in(credentials, monkeypatch):
    calls = []
    replies = iter(
        [
            {"access_token": "temporary-device-fixture"},
            {"changed": True, "signed_out": True},
            {"access_token": "fresh-device-fixture", "user": {"must_change_password": False}},
        ]
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(next(replies)).encode()

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return Response()

    monkeypatch.setattr(smoke_auth.urllib.request, "build_opener", lambda *args: Opener())
    assert (
        smoke_auth.authenticate("http://localhost:8080", "smoke-admin", credentials, initialize=True)
        == "fresh-device-fixture"
    )
    assert [request.full_url.rsplit("/", 1)[-1] for request in calls] == ["login", "change-password", "login"]
    first, change, final = [json.loads(request.data) for request in calls]
    assert first == {"username": "smoke-admin", "password": "temporary-bootstrap-fixture", "native": True}
    assert change == {
        "current_password": "temporary-bootstrap-fixture",
        "password": "replacement-password-fixture",
    }
    assert calls[1].get_header("Authorization") == "Bearer temporary-device-fixture"
    assert final["password"] == "replacement-password-fixture" and final["native"] is True


def test_cli_signs_out_without_writing_or_printing_tokens(credentials, monkeypatch, capsys):
    monkeypatch.setattr(smoke_auth, "authenticate", lambda *args, **kwargs: "secret-device-fixture")

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.endswith("/api/auth/logout")
            assert request.get_header("Authorization") == "Bearer secret-device-fixture"
            return io.BytesIO(b"{}")

    monkeypatch.setattr(smoke_auth.urllib.request, "build_opener", lambda *args: Opener())
    assert smoke_auth.main(["--directory", str(credentials)]) == 0
    assert not (credentials / "device-token").exists()
    assert capsys.readouterr().out == ""


def test_cli_errors_never_print_credentials_or_response_bodies(credentials, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise urllib.error.URLError("secret-password-fixture")

    monkeypatch.setattr(smoke_auth, "authenticate", fail)
    assert smoke_auth.main(["--directory", str(credentials)]) == 1
    output = capsys.readouterr()
    assert "secret-password" not in output.out + output.err
    assert not (credentials / "device-token").exists()


@pytest.mark.parametrize(
    "base", ["http://remote.test", "https://user:password@example.test", "https://example.test/?secret=1"]
)
def test_cli_rejects_remote_http_or_credential_bearing_origins(credentials, base):
    with pytest.raises(ValueError):
        smoke_auth.authenticate(base, "smoke-admin", credentials)


def test_workflow_uses_real_bootstrap_without_legacy_tokens():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/smoke.yml").read_text()
    assert "APP_TOKEN" not in workflow
    assert "DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD_FILE=" in workflow
    assert "DENKZEUG_AUTH_ALLOW_INSECURE_LOCALHOST=true" in workflow
    assert "scripts/smoke_auth.py --initialize" in workflow
    assert "X-Internal-Token: $INTERNAL_TOKEN" in workflow
