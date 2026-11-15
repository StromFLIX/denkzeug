"""Fail-closed route coverage, scoped keys, ownership, webhook and audit contracts."""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from nautionette_backend import api_keys, main
from nautionette_backend.api_policy import POLICIES, validate_routes
from nautionette_backend.auth_store import digest
from nautionette_backend.config import settings
from nautionette_backend.security import Principal


def create(client, scopes=("system:read",), **kwargs):
    result = client.post(
        "/api/tokens",
        json={
            "name": "CI integration",
            "grants": [{"scope": scope, "resources": None} for scope in scopes],
            **kwargs,
        },
    )
    assert result.status_code == 201, result.text
    return result.json()


def external(secret):
    return TestClient(main.app, headers={"Authorization": f"Bearer {secret}"})


def test_every_operation_has_a_reviewed_policy():
    validate_routes(main.app)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/unreviewed")
    def endpoint():
        return {}

    with pytest.raises(RuntimeError, match="no explicit authentication"):
        validate_routes(app)
    assert POLICIES["POST /api/workflows/{name}/run"].scope == "workflows:trigger"
    assert POLICIES["POST /api/workflows/{name}/deploy"].scope == "workflows:write"
    assert POLICIES["WEBSOCKET /api/speech/stream"].authentication == "ticket"


def test_new_runtime_route_is_denied_even_after_startup(client):
    main.app.add_api_route("/api/unreviewed-test", lambda: {"leak": True}, methods=["GET"])
    route = main.app.router.routes.pop()
    main.app.router.routes.insert(0, route)
    try:
        result = client.get("/api/unreviewed-test")
        assert result.status_code == 403
    finally:
        main.app.router.routes.remove(route)


def test_secret_revealed_once_and_scopes_apply_to_admin_keys(client, db):
    created = create(client)
    raw, token = created["secret"], created["token"]
    row = db.one("SELECT * FROM api_keys WHERE id=?", (token["id"],))
    assert row["secret_hash"] == digest(raw) and raw not in json.dumps(row)
    http = external(raw)
    assert http.get("/api/system").status_code == 200
    assert http.get("/api/settings").status_code == 403
    assert http.post("/api/system/worker/restart").status_code == 403
    assert http.get("/api/auth/me").status_code == 403
    assert http.get("/api/tokens").status_code == 403
    listed = client.get("/api/tokens").json()
    assert raw not in json.dumps(listed)
    assert next(item for item in listed["tokens"] if item["id"] == token["id"])["requests_7d"] == 5


def test_expiry_revocation_and_rotation_are_immediate(client, db):
    created = create(client)
    token_id = created["token"]["id"]
    rotated = client.post(f"/api/tokens/{token_id}/rotate")
    assert rotated.status_code == 201, rotated.text
    assert external(created["secret"]).get("/api/system").status_code == 401
    replacement = rotated.json()
    assert external(replacement["secret"]).get("/api/system").status_code == 200
    db.execute("UPDATE api_keys SET expires_at=? WHERE id=?", (time.time() - 1, replacement["token"]["id"]))
    assert external(replacement["secret"]).get("/api/system").status_code == 401
    another = create(client)
    assert client.delete(f"/api/tokens/{another['token']['id']}").status_code == 200
    assert external(another["secret"]).get("/api/system").status_code == 401


@pytest.mark.parametrize("expiry", [0, -1, float("inf"), time.time() + 400 * 86400])
def test_invalid_expiry_rejected(client, expiry):
    if not __import__("math").isfinite(expiry):
        with pytest.raises(HTTPException):
            api_keys.issue(Principal("test-admin", "admin"), "bad", [{"scope": "system:read"}], expiry)
    else:
        assert (
            client.post(
                "/api/tokens",
                json={"name": "bad", "expires_at": expiry, "grants": [{"scope": "system:read"}]},
            ).status_code
            == 422
        )


def test_key_cannot_mint_keys_or_override_bad_bearer_with_cookie(client):
    created = create(client, ("tokens:read", "tokens:manage"))
    http = external(created["secret"])
    assert http.get("/api/tokens").status_code == 200
    assert (
        http.post("/api/tokens", json={"name": "escape", "grants": [{"scope": "system:control"}]}).status_code
        == 403
    )
    # A valid session does not rescue an explicitly invalid key.
    assert client.get("/api/system", headers={"Authorization": "Bearer dz_key_invalid"}).status_code == 401


def test_usage_is_complete_and_secret_free_including_denials(client, db):
    created = create(client)
    raw, key = created["secret"], created["token"]["id"]
    http = external(raw)
    assert http.get("/api/system", params={"token": "do-not-store-query"}).status_code == 200
    assert http.get("/api/settings").status_code == 403
    db.execute("UPDATE api_keys SET expires_at=? WHERE id=?", (time.time() - 1, key))
    assert http.get("/api/system").status_code == 401
    history = client.get(f"/api/tokens/{key}/requests").json()
    assert history["total"] == 3
    assert {item["status"] for item in history["requests"]} == {200, 401, 403}
    assert raw not in json.dumps(history) and "do-not-store-query" not in json.dumps(history)
    assert client.get(f"/api/tokens/{key}/requests?limit=1").json()["total"] == 3
    usage = client.get(f"/api/tokens/{key}/usage").json()
    assert sum(item["requests"] for item in usage["days"]) == 3
    assert sum(item["failures"] for item in usage["days"]) == 2
    assert usage["retention"] == {"detail_days": 90, "aggregate_days": 365}


def test_tokens_and_audit_are_private_even_from_another_admin(client, db):
    created = create(client)
    key = created["token"]["id"]
    now = time.time()
    db.execute(
        "INSERT INTO auth_users(id,username,display_name,role,created_at,updated_at) "
        "VALUES('other','other','Other','admin',?,?)",
        (now, now),
    )
    db.execute("UPDATE api_keys SET owner_user_id='other' WHERE id=?", (key,))
    assert key not in {item["id"] for item in client.get("/api/tokens").json()["tokens"]}
    for path in [f"/api/tokens/{key}/requests", f"/api/tokens/{key}/usage"]:
        assert client.get(path).status_code == 404
    assert client.delete(f"/api/tokens/{key}").status_code == 404
    assert client.post(f"/api/tokens/{key}/rotate").status_code == 404


def test_owner_suspension_and_resource_restrictions(client, db):
    chat_a = db.create_chat("A", "default")
    chat_b = db.create_chat("B", "default")
    created = client.post(
        "/api/tokens",
        json={"name": "One chat", "grants": [{"scope": "chats:read", "resources": [chat_a["id"]]}]},
    ).json()
    http = external(created["secret"])
    assert http.get(f"/api/chats/{chat_a['id']}").status_code == 200
    assert http.get(f"/api/chats/{chat_b['id']}").status_code == 403
    assert http.get("/api/chats").status_code == 403  # Never leak unfiltered collections.
    from nautionette_backend.account_streams import live

    who = Principal("test-admin", "admin", token_id=created["token"]["id"])
    assert live(who)
    db.execute("UPDATE auth_users SET status='suspended' WHERE id='test-admin'")
    assert http.get(f"/api/chats/{chat_a['id']}").status_code == 401
    assert not live(who)


def test_copy_webhook_creates_exact_temporary_trigger_permission(client, db, backend, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://testserver")
    # FakeAuthoring has the deployed test workflows.
    backend.authoring.workflows["hook-a"] = {"name": "hook-a", "code": "", "manifest": {}}
    backend.authoring.workflows["hook-b"] = {"name": "hook-b", "code": "", "manifest": {}}
    result = client.post("/api/workflows/hook-a/webhook-token", json={"days": 7})
    assert result.status_code == 201, result.text
    created = result.json()
    assert created["token"]["kind"] == "webhook"
    assert created["token"]["grants"] == [
        {"scope": "workflows:trigger", "resources": [api_keys.workflow_identity("hook-a")]}
    ]
    assert 6.99 * 86400 < created["expires_at"] - time.time() <= 7 * 86400
    http = TestClient(main.app)
    fired = http.post(created["url"], json={})
    assert fired.status_code == 200, fired.text
    assert http.post("/api/triggers/hook-b", params={"token": created["secret"]}, json={}).status_code == 403
    api = external(created["secret"])
    assert api.get("/api/workflows/hook-a").status_code == 403
    assert api.post("/api/workflows/hook-a/run", json={}).status_code == 403
    assert api.post("/api/triggers/hook-a", content="not json").status_code == 422
    assert api.post("/api/triggers/hook-a", content=b"x" * 1048577).status_code == 413
    history = client.get(f"/api/tokens/{created['token']['id']}/requests").json()
    assert any(item["run_id"] == fired.json()["workflow_id"] for item in history["requests"])
    db.forget_workflow("hook-a")
    api_keys.workflow_identity("hook-a")
    assert http.post(created["url"], json={}).status_code == 403
    assert created["secret"] not in json.dumps(history)


def test_api_key_external_mcp_never_gets_internal_authority(client):
    created = create(client, ("mcp:read", "mcp:execute", "system:read"))
    request = __import__("starlette.requests", fromlist=["Request"]).Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/",
            "headers": [(b"authorization", f"Bearer {created['secret']}".encode())],
            "query_string": b"",
        }
    )
    context = SimpleNamespace(request=request)
    import asyncio

    tools = asyncio.run(main.backend_mcp.list_tools(context, None))
    assert "system_status" in {tool.name for tool in tools.tools}
    assert "restart_workers" not in {tool.name for tool in tools.tools}
    from mcp import types

    ok = asyncio.run(
        main.backend_mcp.call_tool(context, types.CallToolRequestParams(name="system_status", arguments={}))
    )
    assert not ok.is_error
    denied = asyncio.run(
        main.backend_mcp.call_tool(context, types.CallToolRequestParams(name="restart_workers", arguments={}))
    )
    assert denied.is_error
    forged = asyncio.run(
        main.backend_mcp.call_tool(
            context,
            types.CallToolRequestParams(name="system_status", arguments={"__denkzeug_actor": "forged"}),
        )
    )
    assert forged.is_error


def test_access_log_drops_query_secrets():
    from nautionette_backend.access_logging import SafeAccessFilter

    record = logging.LogRecord(
        "uvicorn.access",
        20,
        "",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "POST", "/api/triggers/demo?token=never-log-this", "1.1", 200),
        None,
    )
    assert SafeAccessFilter().filter(record)
    assert "never-log-this" not in record.getMessage()


@pytest.mark.asyncio
async def test_real_mcp_transport_enforces_scopes_and_audits_rpc_denials(client, db):
    import httpx

    created = create(client, ("mcp:read", "mcp:execute", "system:read"))
    headers = {
        "Authorization": f"Bearer {created['secret']}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    async with main.backend_mcp.http_app.router.lifespan_context(main.backend_mcp.http_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://testserver", headers=headers
        ) as http:
            result = await http.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert result.status_code == 200, result.text
            result = await http.post(
                "/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            )
            assert result.status_code == 200, result.text
            assert "system_status" in {tool["name"] for tool in result.json()["result"]["tools"]}
            result = await http.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "system_status", "arguments": {}},
                },
            )
            assert result.status_code == 200 and not result.json()["result"].get("isError"), result.text
            result = await http.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "restart_workers", "arguments": {}},
                },
            )
            assert result.status_code == 200 and result.json()["result"]["isError"], result.text
    history = client.get(f"/api/tokens/{created['token']['id']}/requests").json()["requests"]
    assert any(row["tool"] == "restart_workers" and row["outcome"] == "failed" for row in history)


def test_preflight_is_public_but_only_for_reviewed_operations(anonymous, monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", ["https://native.example"])
    # CORS middleware is already built with configured origins; policy admission
    # must let it respond (possibly a CORS rejection), not return API authentication.
    result = anonymous.options(
        "/api/system",
        headers={
            "Origin": "https://native.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert result.status_code in {200, 400}
    assert (
        anonymous.options(
            "/api/unknown",
            headers={
                "Origin": "https://native.example",
                "Access-Control-Request-Method": "GET",
            },
        ).status_code
        == 404
    )


def test_key_catalog_has_delegable_scopes_and_zero_usage_for_new_keys(client):
    result = client.get("/api/tokens/catalog")
    assert result.status_code == 200, result.text
    scopes = {item["scope"] for item in result.json()["scopes"]}
    assert "workflows:trigger" in scopes and "mcp:execute" in scopes
    assert "accounts:manage" not in scopes and "tokens:manage" not in scopes
    created = create(client)
    assert created["token"]["requests_7d"] == created["token"]["failures_7d"] == 0


def test_members_lose_key_execution_when_workflow_share_is_removed(client, db, backend):
    now = time.time()
    db.execute(
        "INSERT INTO auth_users(id,username,display_name,role,created_at,updated_at) "
        "VALUES('member','member','Member','member',?,?)",
        (now, now),
    )
    db.execute(
        "INSERT INTO access_grants(kind,resource_id,user_id,created_at) "
        "VALUES('workflow','shared-flow','member',?)",
        (now,),
    )
    backend.authoring.workflows["shared-flow"] = {"name": "shared-flow", "code": "", "manifest": {}}
    generation = api_keys.workflow_identity("shared-flow")
    created = api_keys.issue(
        Principal("member", "member"),
        "Shared trigger",
        [{"scope": "workflows:trigger", "resources": [generation]}],
    )
    http = external(created["secret"])
    assert http.post("/api/workflows/shared-flow/run", json={}).status_code == 200
    db.execute("DELETE FROM access_grants WHERE resource_id='shared-flow'")
    assert http.post("/api/workflows/shared-flow/run", json={}).status_code == 404
    with pytest.raises(HTTPException):
        api_keys.issue(Principal("member", "member"), "Escalate", [{"scope": "system:control"}])


def test_unclassified_websockets_and_mounted_children_fail_closed(client):
    from starlette.websockets import WebSocketDisconnect

    async def socket(websocket):
        await websocket.accept()

    main.app.add_api_websocket_route("/api/unreviewed-socket", socket)
    route = main.app.router.routes[-1]
    try:
        with pytest.raises(WebSocketDisconnect) as result:
            with client.websocket_connect("/api/unreviewed-socket"):
                pass
        assert result.value.code == 1008
    finally:
        main.app.router.routes.remove(route)
    mounted = main.backend_mcp.http_app
    from starlette.routing import Route

    child = Route("/unreviewed", lambda request: None, methods=["GET"])
    mounted.router.routes.append(child)
    try:
        with pytest.raises(RuntimeError, match="no explicit authentication"):
            validate_routes(main.app)
    finally:
        mounted.router.routes.remove(child)


def test_metadata_keys_cannot_become_admin_agent_turn_capabilities(client, db):
    created = create(client, ("chats:write", "workflows:write"))
    chat = db.create_chat("Metadata only", "default")
    http = external(created["secret"])
    for action in ("messages", "queue/resume", "promote"):
        result = http.post(
            f"/api/chats/{chat['id']}/{action}", json={"text": "Change unrelated backend settings"}
        )
        assert result.status_code == 403, result.text
    assert db.query("SELECT * FROM chat_turns WHERE chat_id=?", (chat["id"],)) == []


def test_openapi_documents_the_final_scope_policies(client):
    document = client.get("/api/openapi.json").json()
    policy = document["paths"]["/api/workflows/{name}/run"]["post"]["x-denkzeug-policy"]
    assert policy == {"scope": "workflows:trigger", "authentication": "account", "resource": "workflow:name"}
