"""Deployment and local-operator auth boundaries, without production secrets."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import coolify_release

ROOT = Path(__file__).resolve().parents[1]
# Load only the CLI: --help and argument handling must not initialize a database.
SPEC = importlib.util.spec_from_file_location(
    "nautionette_backend.auth_cli",
    ROOT / "services/backend/nautionette_backend/auth_cli.py",
)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


@pytest.fixture
def store(monkeypatch):
    module = types.ModuleType("nautionette_backend.auth_store")
    module.issue_setup_token = Mock(return_value="single-use-setup-fixture")
    module.recover_password = Mock()
    monkeypatch.setitem(sys.modules, "nautionette_backend.auth_store", module)
    return module


def test_setup_token_is_only_printed_on_explicit_operator_invocation(store, capsys):
    assert cli.main(["setup-token"]) == 0
    assert capsys.readouterr().out == "single-use-setup-fixture\n"
    store.issue_setup_token.assert_called_once_with()


def test_password_file_recovery_preserves_spaces_and_hides_password(store, tmp_path, capsys):
    secret = tmp_path / "secret"
    secret.write_text(" valid-secret-with-spaces \n")
    assert cli.main(["recover-password", "owner", "--password-file", str(secret)]) == 0
    store.recover_password.assert_called_once_with("owner", " valid-secret-with-spaces ")
    output = capsys.readouterr()
    assert "valid-secret" not in output.out + output.err
    assert "sessions revoked" in output.out


def test_recovery_failure_does_not_echo_secret_exception(store, tmp_path, capsys):
    secret = tmp_path / "secret"
    secret.write_text("fixture-password")
    store.recover_password.side_effect = ValueError("credential-do-not-log")
    assert cli.main(["recover-password", "owner", "--password-file", str(secret)]) == 1
    output = capsys.readouterr()
    assert "credential-do-not-log" not in output.out + output.err
    assert "fixture-password" not in output.out + output.err


def test_recovery_never_accepts_password_in_arguments(store):
    with pytest.raises(SystemExit) as caught:
        cli.main(["recover-password", "owner", "--password", "unsafe"])
    assert caught.value.code == 2
    store.recover_password.assert_not_called()


def test_noninteractive_recovery_requires_file(store, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as caught:
        cli.main(["recover-password", "owner"])
    assert caught.value.code == 2
    store.recover_password.assert_not_called()


def test_interactive_recovery_confirms_password(store, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: "fixture-valid-password")
    assert cli.main(["recover-password", "owner"]) == 0
    store.recover_password.assert_called_once_with("owner", "fixture-valid-password")


def test_interactive_mismatch_never_mutates_store(store, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    answers = iter(["fixture-valid-password", "different-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(answers))
    with pytest.raises(SystemExit):
        cli.main(["recover-password", "owner"])
    store.recover_password.assert_not_called()


def test_compose_passes_only_backend_identity_and_smtp_configuration():
    compose = (ROOT / "docker-compose.yaml").read_text()
    backend = compose.split("  backend:\n", 1)[1].split("  website:\n", 1)[0]
    for key in (
        "DENKZEUG_PUBLIC_URL",
        "DENKZEUG_BOOTSTRAP_ADMIN_USERNAME",
        "DENKZEUG_BOOTSTRAP_ADMIN_EMAIL",
        "DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD_FILE",
        "DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD",
        "DENKZEUG_AUTH_ALLOW_INSECURE_LOCALHOST",
        "DENKZEUG_SMTP_HOST",
        "DENKZEUG_SMTP_PORT",
        "DENKZEUG_SMTP_USERNAME",
        "DENKZEUG_SMTP_PASSWORD_FILE",
        "DENKZEUG_SMTP_PASSWORD",
        "DENKZEUG_SMTP_FROM",
        "DENKZEUG_SMTP_TLS_MODE",
    ):
        assert key + ":" in backend
        assert key + ":" not in compose.replace(backend, "")
    assert "APP_TOKEN" not in compose
    assert "${INTERNAL_TOKEN:?" in backend
    assert "DENKZEUG_AUTH_ALLOW_INSECURE_LOCALHOST:-false" in backend


def test_legacy_token_never_satisfies_release_health_credentials(monkeypatch):
    monkeypatch.setenv("APP_URL", "https://example.test")
    monkeypatch.setenv("APP_TOKEN", "obsolete-token")
    monkeypatch.delenv("INTERNAL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="INTERNAL_TOKEN"):
        coolify_release.healthy("staging")


def test_service_request_uses_internal_header_not_user_bearer(monkeypatch):
    captured = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{}'

    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            return Response()

    monkeypatch.setattr(coolify_release.urllib.request, "build_opener", lambda *args: Opener())
    coolify_release.request("https://example.test/internal/system", "service-fixture", internal=True)
    headers = {key.lower(): value for key, value in captured[0].header_items()}
    assert headers["x-internal-token"] == "service-fixture"
    assert "authorization" not in headers
