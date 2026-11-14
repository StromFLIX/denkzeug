"""Workflow MCP is not a backdoor from model tools to unscoped service authority."""

import json
from contextlib import contextmanager

import httpx
from fastapi.testclient import TestClient
from nautionette_backend import chat_outputs, main
from nautionette_backend.security import Principal, current_principal
from nautionette_workflow_mcp import backend as authoring_backend
from nautionette_workflow_mcp import main as authoring_main

from ..conftest import INTERNAL_TOKEN


@contextmanager
def actor(db, tools=None):
    chat = db.create_chat("Scoped authoring", "default")
    message, _ = db.accept_chat_message(chat["id"], "Manage my workflows", "authoring-turn")
    db.execute("UPDATE chat_turns SET job=? WHERE id=?", (json.dumps({"tools": tools}), message["id"]))
    with chat_outputs.publication_grant(chat["id"], message["id"]) as grant:
        yield grant["grant"]


def call(client, tool, grant=None, arguments=None):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": {**(arguments or {}), **({"__denkzeug_actor": grant} if grant else {})},
            },
        },
    )


def bridge(monkeypatch):
    async def forward(path, payload):
        async with httpx.ASGITransport(app=main.app) as transport:
            request = httpx.Request(
                "POST", "http://backend" + path, headers={"X-Internal-Token": INTERNAL_TOKEN}, json=payload
            )
            response = await transport.handle_async_request(request)
            response.request = request
            await response.aread()
            response.raise_for_status()
            return response.json()

    monkeypatch.setattr(authoring_backend, "post", forward)


def test_workflow_service_rest_requires_separate_service_credentials(backend):
    client = TestClient(authoring_main.app)
    assert client.get("/api/workflows").status_code == 401
    assert client.post("/api/validate", json={}).status_code == 401
    assert client.delete("/api/workflows/anything").status_code == 401


def test_unscoped_mcp_cannot_read_or_deploy_workflows(backend, monkeypatch):
    def no_service_call(*args, **kwargs):
        raise AssertionError("missing capabilities must not dispatch service operations")

    monkeypatch.setattr(authoring_backend, "post", no_service_call)
    client = TestClient(authoring_main.app)
    for tool, arguments in [
        ("list_workflows", {}),
        ("list_runs", {}),
        ("write_workflow", {"name": "safe", "code": "MANIFEST={}"}),
    ]:
        response = call(client, tool, arguments=arguments)
        assert response.status_code == 200 and response.json()["result"]["isError"]


def test_workflow_mcp_history_is_private_even_for_an_admin_agent(backend, db, monkeypatch):
    bridge(monkeypatch)
    db.record_run("job", "my-run", None, "manual", {"owned": "visible"})
    db.execute(
        "INSERT INTO auth_users(id,username,display_name,role,created_at,updated_at) "
        "VALUES('other','other','Other','admin',0,0)"
    )
    reset = current_principal.set(Principal("other", "admin"))
    try:
        db.record_run("job", "other-run", None, "manual", {"secret": "other-persons-input"})
    finally:
        current_principal.reset(reset)
    client = TestClient(authoring_main.app)
    with actor(db) as grant:
        response = call(client, "list_runs", grant)
        assert response.status_code == 200 and not response.json()["result"]["isError"]
        text = response.json()["result"]["content"][0]["text"]
        assert "my-run" in text and "other-run" not in text and "other-persons-input" not in text
        forbidden = call(client, "read_run", grant, {"workflow_id": "other-run"})
        assert forbidden.json()["result"]["isError"]
        assert "other-persons-input" not in forbidden.text
    expired = call(client, "list_runs", grant)
    assert expired.json()["result"]["isError"]


def test_workflow_mcp_cannot_escape_the_turn_tool_allowlist(backend, db, monkeypatch):
    bridge(monkeypatch)
    client = TestClient(authoring_main.app)
    with actor(db, ["workflows_list_runs"]) as grant:
        assert not call(client, "list_runs", grant).json()["result"]["isError"]
        denied = call(client, "write_workflow", grant, {"name": "escape", "code": "MANIFEST={}"})
        assert denied.json()["result"]["isError"]
        assert not backend.authoring.workflows


def test_scoped_workflow_mcp_still_deploys_for_an_authorized_admin(backend, db, monkeypatch):
    bridge(monkeypatch)
    client = TestClient(authoring_main.app)
    with actor(db, ["workflows_write_workflow", "workflows_read_workflow"]) as grant:
        response = call(client, "write_workflow", grant, {"name": "safe", "code": "MANIFEST={}"})
        assert not response.json()["result"]["isError"], response.text
        result = json.loads(response.json()["result"]["content"][0]["text"])
        assert result["published"] and result["ready"]
        read = call(client, "read_workflow", grant, {"name": "safe"})
        assert not read.json()["result"]["isError"], read.text
        assert "MANIFEST={}" in read.json()["result"]["content"][0]["text"]
