"""Refreshing QA accounts use normal API sessions and never persist/log tokens."""

import io
import json
import urllib.error
import urllib.request

import httpx
import pytest

from scripts import account_session as sessions


def issued(number=1, **extra):
    return {
        "access_token": f"private-access-{number}",
        "refresh_token": f"private-refresh-{number}",
        "expires_in": 600,
        "user": {"must_change_password": False},
        **extra,
    }


class Opener:
    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []

    def open(self, request, timeout):
        self.calls.append(
            (
                request.full_url,
                request.get_header("Authorization"),
                json.loads(request.data) if request.data else None,
            )
        )
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())


def session(opener):
    return sessions.AccountSession(
        "https://qa.test", username="qa", password="private-password", opener=opener
    )


def test_login_proactive_refresh_rotates_credentials_and_logout(monkeypatch, capsys):
    now = {"time": 0}
    monkeypatch.setattr(sessions.time, "monotonic", lambda: now["time"])
    opener = Opener([issued(), {}, issued(2), {}, {}])
    with session(opener) as account:
        account.open(urllib.request.Request("https://qa.test/api/system")).close()
        now["time"] = 571
        account.open(urllib.request.Request("https://qa.test/api/system")).close()
    assert [row[0].rsplit("/", 1)[-1] for row in opener.calls] == [
        "login",
        "system",
        "refresh",
        "system",
        "logout",
    ]
    assert opener.calls[0][2] == {"username": "qa", "password": "private-password", "native": True}
    assert opener.calls[1][1] == "Bearer private-access-1"
    assert opener.calls[2][2] == {"refresh_token": "private-refresh-1"}
    assert opener.calls[3][1] == opener.calls[4][1] == "Bearer private-access-2"
    assert account._token == account._refresh == ""
    assert capsys.readouterr().out == ""


def test_definite_401_refreshes_and_retries_once_only():
    def unauthorized():
        return urllib.error.HTTPError("https://qa.test/api/system", 401, "private-error", {}, None)

    opener = Opener([issued(), unauthorized(), issued(2), unauthorized()])
    account = session(opener)
    with pytest.raises(urllib.error.HTTPError):
        account.open(urllib.request.Request("https://qa.test/api/system"))
    assert len(opener.calls) == 4
    assert opener.calls[-1][1] == "Bearer private-access-2"


def test_uncertain_refresh_is_never_replayed_or_echoed(monkeypatch):
    opener = Opener([issued(), TimeoutError("private-provider-echo")])
    account = session(opener)
    with pytest.raises(sessions.SessionError) as error:
        account.refresh()
    assert "private-provider" not in str(error.value)
    with pytest.raises(sessions.SessionError):
        account.refresh()
    assert len(opener.calls) == 2


def test_403_or_transport_failure_does_not_replay_mutation():
    failure = urllib.error.HTTPError("https://qa.test/api/chats", 403, "private-error", {}, None)
    opener = Opener([issued(), failure])
    account = session(opener)
    with pytest.raises(urllib.error.HTTPError):
        account.open(urllib.request.Request("https://qa.test/api/chats", method="POST", data=b"{}"))
    assert len(opener.calls) == 2


def test_temporary_account_is_signed_out_not_reset():
    opener = Opener([issued(user={"must_change_password": True}), {}])
    with pytest.raises(sessions.SessionError, match="password change"):
        session(opener)
    assert [row[0].rsplit("/", 1)[-1] for row in opener.calls] == ["login", "logout"]


def test_manual_token_requires_explicit_opt_in_and_never_uses_refresh(monkeypatch):
    monkeypatch.setenv("DENKZEUG_DEVICE_TOKEN", "manual-private-token")
    monkeypatch.delenv("DENKZEUG_ALLOW_DEVICE_TOKEN", raising=False)
    with pytest.raises(sessions.SessionError, match="DENKZEUG_ALLOW_DEVICE_TOKEN"):
        sessions.AccountSession.from_env("https://qa.test")
    monkeypatch.setenv("DENKZEUG_ALLOW_DEVICE_TOKEN", "true")
    opener = Opener([])
    with sessions.AccountSession.from_env("https://qa.test", opener=opener) as account:
        assert account.authorization() == "Bearer manual-private-token"
        with pytest.raises(sessions.SessionError, match="expired or was rejected"):
            account.refresh()
    assert opener.calls == []  # Do not revoke a human's explicit manual session.


def test_password_file_and_foreign_origin_guard(monkeypatch, tmp_path):
    monkeypatch.delenv("DENKZEUG_DEVICE_TOKEN", raising=False)
    monkeypatch.setenv("DENKZEUG_SMOKE_USERNAME", "qa")
    monkeypatch.delenv("DENKZEUG_SMOKE_PASSWORD", raising=False)
    password = tmp_path / "password"
    password.write_text(" private-passphrase \n")
    monkeypatch.setenv("DENKZEUG_SMOKE_PASSWORD_FILE", str(password))
    opener = Opener([issued(), {}])
    with sessions.AccountSession.from_env("https://qa.test", opener=opener) as account:
        assert opener.calls[0][2]["password"] == " private-passphrase "
        with pytest.raises(sessions.SessionError, match="another origin"):
            account.open(urllib.request.Request("https://foreign.test/api/system"))
    assert len(opener.calls) == 2


def test_httpx_adapter_reactive_refresh_and_origin_guard():
    opener = Opener([issued(), issued(2), {}])
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={})

    with (
        session(opener) as account,
        httpx.Client(
            base_url="https://qa.test", auth=account.httpx_auth(), transport=httpx.MockTransport(handler)
        ) as client,
    ):
        assert client.get("/api/system").status_code == 200
        with pytest.raises(sessions.SessionError, match="another origin"):
            client.get("https://foreign.test/api/system")
    assert seen == ["Bearer private-access-1", "Bearer private-access-2"]


def test_login_failure_has_no_credential_or_response_diagnostics(capsys):
    opener = Opener([urllib.error.HTTPError("https://qa.test", 401, "private-password", {}, None)])
    with pytest.raises(sessions.SessionError) as error:
        session(opener)
    assert "private-password" not in str(error.value)
    assert capsys.readouterr().out == ""
