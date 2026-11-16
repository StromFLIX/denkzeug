"""Deployment checks retain authenticated transport without a feature dependency."""

import io
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import smoke_api


@pytest.mark.parametrize(
    "url",
    [
        "http://app.test",
        "https://user:secret@app.test",
        "https://app.test/?token=bad",
        "https://app.test/#secret",
        "https://",
    ],
)
def test_only_direct_https_origins_are_accepted(monkeypatch, url):
    monkeypatch.setenv("APP_URL", url)
    with pytest.raises(ValueError, match="direct HTTPS"):
        smoke_api.API()


def session(monkeypatch):
    monkeypatch.setenv("APP_URL", "https://app.test/")
    transport = MagicMock()
    monkeypatch.setattr(smoke_api.AccountSession, "from_env", lambda url: transport)
    return transport


def test_smoke_uses_account_session_and_closes_it(monkeypatch):
    transport = session(monkeypatch)
    response = MagicMock()
    response.__enter__.return_value = SimpleNamespace(status=200, read=lambda: b'{"ready":true}')
    transport.open.return_value = response
    with smoke_api.API() as api:
        assert api.call("/api/workflows/check/deploy", "POST", {"code": "test"}) == {"ready": True}
    request = transport.open.call_args.args[0]
    assert request.method == "POST"
    assert request.full_url == "https://app.test/api/workflows/check/deploy"
    assert request.data == b'{"code": "test"}'
    transport.open.assert_called_once()
    transport.close.assert_called_once()


def test_expected_http_error_is_not_a_transport_failure(monkeypatch):
    transport = session(monkeypatch)
    transport.open.side_effect = urllib.error.HTTPError("https://app.test", 409, "conflict", {}, io.BytesIO())
    with smoke_api.API() as api:
        assert api.call("/api/workflows/check/run", "POST", {}, expected=409) is None
    transport.open.assert_called_once()


@pytest.mark.parametrize("error", [urllib.error.URLError("private failure detail"), TimeoutError()])
def test_uncertain_mutations_are_never_replayed_or_logged(monkeypatch, error):
    transport = session(monkeypatch)
    transport.open.side_effect = error
    with smoke_api.API() as api, pytest.raises(RuntimeError, match="never replay") as caught:
        api.call("/api/workflows/check/run", "POST", {})
    assert "private failure detail" not in str(caught.value)
    transport.open.assert_called_once()
    transport.close.assert_called_once()
