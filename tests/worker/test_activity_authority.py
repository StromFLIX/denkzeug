"""First-party SDK calls carry verified per-tool authority, not a master credential."""

import json
from types import SimpleNamespace

import httpx
import pytest
from nautionette_worker import activities

from ..conftest import INTERNAL_TOKEN


@pytest.mark.parametrize("running", [True, False])
async def test_first_party_activity_scope_is_server_minted_and_revoked(monkeypatch, running):
    info = SimpleNamespace(
        workflow_type="example",
        workflow_id="example-execution",
        workflow_run_id="temporal-run",
        activity_id="step-1",
        attempt=1,
        start_to_close_timeout=None,
        schedule_to_close_timeout=None,
    )
    monkeypatch.setattr(activities.activity, "in_activity", lambda: running)
    monkeypatch.setattr(activities.activity, "info", lambda: info)
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def build_request(self, method, url, *, json, headers):
            return httpx.Request(method, url, json=json, headers=headers)

        async def send(self, request, **_kwargs):
            return await self.post(
                str(request.url), json=json.loads(request.content), headers=request.headers
            )

        async def post(self, url, *, json=None, headers=None, **kwargs):
            calls.append((url, json, dict(headers or {})))
            if url.endswith("/activity-grant"):
                assert headers["X-Internal-Token"] == INTERNAL_TOKEN
                assert json["source"] == {
                    "workflow": "example",
                    "workflow_id": "example-execution",
                    "run_id": "temporal-run",
                    "activity_id": "step-1",
                }
                value = {"grant": "private-activity-scope"}
            elif url.endswith("/revoke-activity-grant"):
                assert json == {"grant": "private-activity-scope"}
                value = {"revoked": True}
            elif url.endswith("/mcp-calls"):
                value = {}
            else:
                assert "X-Internal-Token" not in headers
                assert "Authorization" not in headers
                assert headers[activities.MCP_SCOPE_HEADER] == "backend"
                if json["method"] == "tools/call":
                    assert json["params"]["arguments"]["__denkzeug_actor"] == "private-activity-scope"
                    value = {"result": {"content": [{"type": "text", "text": '{"done":true}'}]}}
                else:
                    value = {"result": {"protocolVersion": "2025-06-18"}}
                value["id"] = json.get("id")
            return httpx.Response(200, json=value, request=httpx.Request("POST", url))

    monkeypatch.setattr(activities.httpx, "AsyncClient", Client)
    params = {
        "tool": "backend_get_example",
        "arguments": {"__denkzeug_actor": "caller-forgery"},
        "source": {"workflow": "forged-caller"},
    }
    if running:
        result = await activities.mcp_call(params)
        assert result["json"] == {"done": True}
        assert any(url.endswith("/revoke-activity-grant") for url, _, _ in calls)
    else:
        with pytest.raises(ValueError, match="provenance"):
            await activities.mcp_call(params)
        assert not any(body and body.get("method") == "tools/call" for _, body, _ in calls)
    assert "caller-forgery" not in json.dumps(calls)
