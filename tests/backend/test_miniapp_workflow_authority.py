"""Mini-app workflow callbacks delegate the saved owner, never the service actor."""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from nautionette_backend import access
from nautionette_backend import miniapp_capabilities as caps
from nautionette_backend.security import Principal, current_principal


@pytest.fixture
def workflow_app(client, db, monkeypatch):
    manifest = client.get("/api/miniapps/contract").json()["example"]
    manifest.pop("backend")
    assert (
        client.put(
            "/api/miniapps/workflow-app", json={"expected_revision": 0, "manifest": manifest}
        ).status_code
        == 200
    )
    db.execute("INSERT INTO access_resources VALUES('workflow','owned_workflow','test-admin')")
    session = caps.Session()
    monkeypatch.setitem(caps.sessions, "workflow-app", session)
    return session


def request(tool="workflows_run_workflow"):
    key = "name" if tool == "workflows_run_workflow" else "workflow"
    return {"kind": "mcp", "tool": tool, "arguments": {key: "owned_workflow"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["workflows_run_workflow", "workflows_list_runs"])
@pytest.mark.parametrize("failed", [False, True])
async def test_workflow_callback_delegates_owner_and_revokes_after_rpc(workflow_app, tool, failed):
    tokens = []

    async def rpc(method, params):
        assert method == "tools/call"
        grant = params["arguments"]["__denkzeug_actor"]
        tokens.append(grant)
        assert access.actor_for_grant(grant).user_id == "test-admin"
        access.authorize_actor_tool(grant, tool)
        with pytest.raises(HTTPException):
            access.authorize_actor_tool(grant, "backend_get_settings")
        if failed:
            raise RuntimeError("upstream failed")
        return {"workflow_id": "smoke-run"}

    workflow_app.rpc = AsyncMock(side_effect=rpc)
    payload = request(tool)
    context = current_principal.set(Principal(None, "service", service=True))
    try:
        if failed:
            with pytest.raises(RuntimeError, match="upstream failed"):
                await caps.mcp("workflow-app", payload)
        else:
            assert await caps.mcp("workflow-app", payload) == {"workflow_id": "smoke-run"}
    finally:
        current_principal.reset(context)
    assert "__denkzeug_actor" not in payload["arguments"]
    assert tokens and tokens[0] not in access._delegations


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["archived", "disabled", "foreign", "unshared_tool"])
async def test_workflow_callback_rechecks_owner_and_resource(workflow_app, db, denial):
    if denial == "archived":
        db.execute("UPDATE mini_apps SET archived=1 WHERE id='workflow-app'")
    elif denial == "disabled":
        db.execute("UPDATE auth_users SET status='suspended' WHERE id='test-admin'")
    elif denial == "foreign":
        db.execute("UPDATE auth_users SET role='member' WHERE id='test-admin'")
    else:
        db.execute("UPDATE auth_users SET role='member' WHERE id='test-admin'")
        db.execute("INSERT INTO access_grants VALUES('workflow','owned_workflow','test-admin',0)")
    workflow_app.rpc = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await caps.mcp("workflow-app", request())
    assert error.value.status_code == (404 if denial == "foreign" else 403)
    workflow_app.rpc.assert_not_called()


@pytest.mark.asyncio
async def test_external_tools_never_receive_account_capability(workflow_app):
    workflow_app.rpc = AsyncMock(return_value={"ok": True})
    payload = {"kind": "mcp", "tool": "github_get_me", "arguments": {}}
    await caps.mcp("workflow-app", payload)
    assert workflow_app.rpc.call_args.args[1] == {"name": "github_get_me", "arguments": {}}


@pytest.mark.asyncio
async def test_workflow_callback_reaches_account_scoped_backend_adapter(workflow_app):
    from nautionette_backend.routers.delegation import WorkflowTool, workflow_call

    async def rpc(method, params):
        arguments = dict(params["arguments"])
        grant = arguments.pop("__denkzeug_actor")
        return await workflow_call(WorkflowTool(tool="list_runs", arguments=arguments, grant=grant))

    workflow_app.rpc = AsyncMock(side_effect=rpc)
    context = current_principal.set(Principal(None, "service", service=True))
    try:
        result = await caps.mcp("workflow-app", request("workflows_list_runs"))
    finally:
        current_principal.reset(context)
    assert not result.get("isError"), result
    assert json.loads(result["content"][0]["text"])["runs"] == []


def test_app_cannot_supply_an_actor_capability():
    payload = request()
    payload["arguments"]["__denkzeug_actor"] = "forged-capability"
    manifest = {"backend": {"permissions": {"mcp": [{"tool": payload["tool"], "arguments": {}}]}}}
    with pytest.raises(HTTPException) as error:
        caps.authorize(manifest, payload)
    assert error.value.status_code == 422
