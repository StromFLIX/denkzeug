from unittest.mock import patch

import pytest

from scripts import check_mcp_apps_deployment as smoke
from scripts.smoke_api import APIError


class Response:
    def __init__(self, url):
        self.url = url
        self.headers = {
            "Content-Security-Policy": "connect-src 'none'; frame-ancestors https://stage.example",
            "X-DNS-Prefetch-Control": "off",
        }

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, _):
        return b"ui/notifications/sandbox-proxy-ready"


class API:
    url = "https://stage.example"

    def __init__(self):
        self.writes = []

    def call(self, path, method="GET", body=None):
        if method != "GET":
            self.writes.append((path, method, body))
        if path == "/api/system":
            return {"environment": "staging"}
        if path == "/api/mcp-apps/runtime":
            return {"sandbox_url": "https://sandbox.example"}
        if path == "/api/apps/deployment-smoke" and method == "GET":
            raise APIError("missing", 404)
        if path == "/api/apps" and method == "POST":
            return {"app": {"active_revision": 1}}
        if path.endswith("/deploy"):
            return {"ready": True}
        if path.endswith("/run"):
            return {"workflow_id": "test-run"}
        if path == "/api/runs/test-run":
            return {"run": {"status": "completed"}}
        if path.startswith("/api/workflows/") and method == "GET":
            return {"settings": {"chat_id": "test-chat"}}
        if path == "/api/chats/test-chat":
            return {
                "messages": [
                    {
                        "content": "App snapshot verified",
                        "meta": {"run": {"workflow_id": "test-run"}, "apps": [{"id": "instance"}]},
                    }
                ]
            }
        if path == "/api/chats/test-chat/apps/instance":
            return {"html": smoke.HTML, "result": {"structuredContent": {"count": 7}}, "revision": 1}
        return {}


def test_staging_app_smoke_uses_credential_free_sandbox_and_cleans_only_own_state():
    api = API()
    with patch("urllib.request.build_opener") as opener:
        opener.return_value.open.side_effect = lambda url, timeout: Response(url)
        smoke.check(api)
    assert ("/api/chats/test-chat", "DELETE", None) in api.writes
    assert api.writes[-1] == (
        "/api/apps/deployment-smoke",
        "PATCH",
        {"archived": True, "expected_revision": 1},
    )
    assert sum(path.endswith("/run") for path, _, _ in api.writes) == 1


def test_app_smoke_refuses_production_before_any_mutation():
    api = API()
    with patch.object(api, "call", return_value={"environment": "production"}):
        with pytest.raises(RuntimeError, match="non-staging"):
            smoke.check(api)
    assert api.writes == []
