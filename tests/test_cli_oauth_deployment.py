import importlib.util
import io
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cli_oauth_check", Path(__file__).resolve().parents[1] / "scripts/check_cli_oauth_deployment.py"
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_readonly_staging_check_does_not_launch_cli_or_log_credentials(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer test-only-token"
            if request.get_method() == "GET":
                assert timeout == 30
                if request.full_url == "https://app.test/api/mcp-oauth/client-metadata":
                    return io.BytesIO(
                        json.dumps(
                            {
                                "client_id": "https://app.test/api/mcp-oauth/client-metadata",
                                "redirect_uris": ["https://app.test/api/mcp-oauth/web-callback"],
                                "token_endpoint_auth_method": "none",
                            }
                        ).encode()
                    )
                assert request.full_url == "https://app.test/api/mcp-oauth/capabilities"
                return io.BytesIO(
                    json.dumps(
                        {
                            "available": True,
                            "desktop_bridge_version": 1,
                            "http": {
                                "available": True,
                                "grant_types": ["authorization_code", "device_code", "client_credentials"],
                                "token_endpoint_auth_methods": [
                                    "none",
                                    "client_secret_basic",
                                    "client_secret_post",
                                    "private_key_jwt",
                                    "client_secret_jwt",
                                ],
                                "web_callback": "https://app.test/api/mcp-oauth/web-callback",
                            },
                        }
                    ).encode()
                )
            assert (
                request.get_method() == "POST"
                and request.full_url == "https://app.test/api/mcp-oauth/discover"
            )
            assert json.loads(request.data) == {"url": "https://workiq.svc.cloud.microsoft/mcp"}
            return io.BytesIO(
                json.dumps(
                    {
                        "url": "https://workiq.svc.cloud.microsoft/mcp",
                        "client_id": "ba081686-5d24-4bc6-a0d6-d034ecffed87",
                        "redirect_uri": "http://localhost:12798",
                    }
                ).encode()
            )

    monkeypatch.setattr(check, "build_opener", lambda *_args: Opener())
    assert check.check("https://app.test", "test-only-token") == {
        "available": True,
        "desktop_bridge_version": 1,
        "https_oauth": True,
        "workiq_discovery": True,
        "generic_oauth_grants": True,
        "confidential_clients": True,
        "web_callback_metadata": True,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"available": False},
        {"available": True, "desktop_bridge_version": 0},
        ["private-detail"],
        "private-detail",
    ],
)
def test_failed_compatibility_never_echoes_response_body(monkeypatch, body):
    class Opener:
        def open(self, *_args, **_kwargs):
            return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(check, "build_opener", lambda *_args: Opener())
    with pytest.raises(RuntimeError) as error:
        check.check("https://app.test", "test-only-token")
    assert "private-detail" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.test",
        "https://user:credential@app.test",
        "file:///private",
        "https://app.test#fragment",
    ],
)
def test_unsafe_origins_are_rejected_before_sending_token(url):
    with pytest.raises(RuntimeError):
        check.check(url, "test-only-token")
