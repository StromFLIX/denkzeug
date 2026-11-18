"""Private app authoring and completion-time, exactly-once workflow snapshots."""

import hashlib
import json
import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from nautionette_backend import access, local_apps, main, run_delivery, runs
from nautionette_backend.api_policy import POLICIES, validate_routes
from nautionette_backend.db import Database
from nautionette_backend.routers.apps import AppPublish
from nautionette_backend.security import Principal, current_principal

from ..conftest import INTERNAL_TOKEN

HTML = "<!doctype html><html><body><h1>Report</h1><script>window.ready=true</script></body></html>"
SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
    "additionalProperties": False,
}


def payload(**changes):
    return {
        "id": "daily-report",
        "title": "Daily report",
        "description": "A report",
        "html": HTML,
        "input_schema": SCHEMA,
        "sample_data": {"count": 1},
        "change_note": "First version",
        "expected_revision": 0,
        **changes,
    }


def publish(**changes):
    return local_apps.publish(AppPublish(**payload(**changes)))


def result(**changes):
    return {"summary": "Two useful items", "data": {"count": 2}, "ui": {"app": "daily-report"}, **changes}


def record(backend, workflow_id="report-1"):
    return backend.db.record_run("report", workflow_id, "temporal-1", "manual", {})


def test_publish_api_immutable_history_archive_and_conflicts(client):
    response = client.post("/api/apps", json=payload())
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["app"] == {
        "id": "daily-report",
        "title": "Daily report",
        "description": "A report",
        "active_revision": 1,
        "archived": False,
        "updated_at": first["app"]["updated_at"],
    }
    assert first["revision"]["html_sha256"] == hashlib.sha256(HTML.encode()).hexdigest()
    assert first["revision"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert client.post("/api/apps", json=payload()).status_code == 409
    assert client.post("/api/apps", json=payload(expected_revision=1, title="Updated")).status_code == 200
    assert client.get("/api/apps/daily-report/revisions/1").json() == first["revision"]
    assert (
        client.patch("/api/apps/daily-report", json={"archived": True, "expected_revision": 1}).status_code
        == 409
    )
    assert (
        client.patch("/api/apps/daily-report", json={"archived": True, "expected_revision": 2}).status_code
        == 200
    )
    assert client.get("/api/apps").json()["apps"][0]["archived"] is True
    assert client.post("/api/apps", json=payload(expected_revision=2)).status_code == 409
    assert (
        client.patch("/api/apps/daily-report", json={"archived": False, "expected_revision": 2}).status_code
        == 200
    )
    assert len(client.get("/api/apps/daily-report").json()["revisions"]) == 2


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"id": "../other"}, 422),
        ({"id": "Capital"}, 422),
        ({"id": "trailing-"}, 422),
        ({"title": " "}, 422),
        ({"expected_revision": True}, 422),
        ({"html": "<div>not a document</div>"}, 422),
        ({"html": '<html><script src="https://evil.test/code.js"></script></html>'}, 422),
        ({"html": '<html><link rel="stylesheet" href="/styles.css"></html>'}, 422),
        ({"html": "<html>" + "é" * 524288 + "</html>"}, 413),
        ({"sample_data": {"count": "wrong"}}, 422),
        ({"input_schema": {"type": "nonsense"}}, 422),
        ({"input_schema": {"$ref": "https://evil.test/schema"}}, 422),
        ({"input_schema": {"$ref": "file:///etc/passwd"}}, 422),
        ({"input_schema": {"$defs": {"unused": {"$ref": "https://evil.test"}}}}, 422),
        ({"input_schema": {"$id": "https://evil.test", "$ref": "#"}}, 422),
        ({"input_schema": {"$dynamicRef": "https://evil.test"}}, 422),
        ({"input_schema": {"$ref": "#/missing"}}, 422),
        ({"input_schema": {"$ref": "#"}}, 422),
    ],
)
def test_reject_invalid_or_remote_app_source(client, changes, code):
    with patch("socket.create_connection", side_effect=AssertionError("Must not fetch schema")):
        response = client.post("/api/apps", json=payload(**changes))
    assert response.status_code == code, response.text
    assert client.get("/api/apps").json() == {"apps": []}


def test_local_references_and_bounded_storage(client, monkeypatch):
    schema = {"$defs": {"report": SCHEMA}, "$ref": "#/$defs/report"}
    assert client.post("/api/apps", json=payload(input_schema=schema)).status_code == 200
    monkeypatch.setattr(local_apps, "MAX_APPS", 1)
    assert client.post("/api/apps", json=payload(id="other")).status_code == 409
    monkeypatch.setattr(local_apps, "MAX_REVISIONS", 1)
    assert client.post("/api/apps", json=payload(expected_revision=1)).status_code == 409
    monkeypatch.setattr(local_apps, "MAX_REVISIONS", 100)
    monkeypatch.setattr(local_apps, "MAX_REGISTRY_BYTES", 1)
    assert client.post("/api/apps", json=payload(expected_revision=1)).status_code == 413
    assert client.get("/api/apps/daily-report").json()["app"]["active_revision"] == 1


def test_owner_private_even_for_admin_and_service(backend):
    publish()
    for who in (Principal("other-admin", "admin"), Principal("member", "member")):
        token = current_principal.set(who)
        try:
            assert local_apps.list_apps() == {"apps": []}
            with pytest.raises(HTTPException) as error:
                local_apps.get_app("daily-report")
            assert error.value.status_code == 404
            # Identical private slugs in two accounts are not a collision.
            assert publish()["app"]["active_revision"] == 1
            local_apps.archive("daily-report", True, 1)
        finally:
            current_principal.reset(token)
    assert local_apps.get_app("daily-report")["app"]["archived"] is False
    token = current_principal.set(Principal("service", "service", service=True))
    try:
        with pytest.raises(HTTPException) as error:
            local_apps.list_apps()
        assert error.value.status_code == 403
    finally:
        current_principal.reset(token)


async def test_workflow_pins_source_data_and_title_at_finish_before_delivery(backend, client, anonymous):
    publish()
    record(backend)
    run_delivery.finish("report-1", "completed", result(), notify=True)
    instance = backend.db.one("SELECT * FROM app_instances")
    assert instance["chat_id"] is None
    assert instance["revision"] == 1
    # A publish, archive, and a process/database restart between finishing and
    # outbox recovery must not change the pinned result or revalidate its source.
    publish(expected_revision=1, title="New report", html="<html>New</html>")
    local_apps.archive("daily-report", True, 2)
    reopened = Database(backend.db.path)
    try:
        assert reopened.one("SELECT revision FROM app_instances")["revision"] == 1
    finally:
        reopened._conn.close()
    # The run view uses the same pinned document before chat delivery and after
    # Temporal history expires. The detail response carries only metadata.
    run_url = "/api/runs/report-1"
    detail = client.get(run_url).json()
    assert detail["history_status"] == "expired"
    assert detail["app"] == {"id": instance["id"], "title": "Daily report"}
    assert anonymous.get(f"{run_url}/app").status_code == 401
    run_app = client.get(f"{run_url}/app")
    assert run_app.status_code == 200
    token = current_principal.set(Principal("other-admin", "admin"))
    try:
        assert local_apps.workflow_view("report-1", "other-admin") == {}
        with pytest.raises(HTTPException) as error:
            local_apps.get_workflow_instance("report-1")
        assert error.value.status_code == 404
    finally:
        current_principal.reset(token)
    await runs.retry_deliveries()
    await runs.retry_deliveries()
    run_delivery.finish("report-1", "completed", result(summary="Overwrite"), notify=True)
    assert len(backend.db.query("SELECT * FROM app_instances")) == 1
    chat = backend.db.list_chats()[0]
    messages = backend.db.list_messages(chat["id"])
    assert len(messages) == 1
    assert messages[0]["content"] == "Two useful items"
    app = messages[0]["meta"]["apps"][0]
    assert app == {"id": instance["id"], "title": "Daily report", "app_id": "daily-report", "revision": 1}
    response = client.get(f"/api/chats/{chat['id']}/apps/{app['id']}")
    assert response.status_code == 200
    value = response.json()
    assert run_app.json() == value
    assert value["html"] == HTML
    assert value["input"] == {}
    assert value["source"] == "workflow"
    assert value["result"] == {
        "content": [{"type": "text", "text": "Two useful items"}],
        "structuredContent": {"count": 2},
    }
    assert backend.db.one("SELECT state FROM run_deliveries")["state"] == "delivered"
    assert backend.db.list_runs()[0]["result"] == result()
    # Run retention must not delete an executable revision already attached to
    # an independent, durable chat message.
    backend.db.execute("DELETE FROM runs WHERE workflow_id='report-1'")
    assert client.get(f"{run_url}/app").status_code == 404
    assert client.get(f"/api/chats/{chat['id']}/apps/{app['id']}").json() == value
    token = current_principal.set(Principal("other-admin", "admin"))
    try:
        with pytest.raises(HTTPException) as error:
            local_apps.get_instance(chat["id"], app["id"])
        assert error.value.status_code == 404
    finally:
        current_principal.reset(token)


@pytest.mark.parametrize(
    "changes",
    [
        {"ui": {"app": "missing"}},
        {"ui": {"app": "daily-report", "revision": 999}},
        {"ui": {"app": "daily-report", "revision": True}},
        {"ui": {"app": "daily-report", "revision": "1"}},
        {"ui": "invalid"},
        {"data": {"count": "wrong"}},
        {"data": []},
    ],
)
async def test_invalid_app_result_degrades_to_summary_without_retry_or_status_loss(backend, changes):
    publish()
    record(backend)
    await runs.complete_run("report", "report-1", "completed", result(**changes))
    assert backend.db.list_runs()[0]["status"] == "completed"
    assert backend.db.list_runs()[0]["result"] == result(**changes)
    assert backend.db.one("SELECT state FROM run_deliveries")["state"] == "delivered"
    assert not backend.db.query("SELECT * FROM app_instances")
    message = backend.db.list_messages(backend.db.list_chats()[0]["id"])[0]
    assert message["content"] == "Two useful items"
    assert message["meta"]["app_diagnostic"] == "invalid_app_result"
    assert "apps" not in message["meta"]


@pytest.mark.parametrize("case", ["archived", "other-owner", "quota"])
async def test_unavailable_app_result_is_a_diagnostic(backend, monkeypatch, case):
    publish()
    if case == "archived":
        local_apps.archive("daily-report", True, 1)
    if case == "other-owner":
        # A run must resolve under its recorded owner, not the current observer.
        record(backend)
        backend.db.execute("UPDATE runs SET owner_user_id='other'")
    else:
        record(backend)
    if case == "quota":
        monkeypatch.setattr(local_apps, "MAX_INSTANCE_BYTES", 1)
    await runs.complete_run("report", "report-1", "completed", result())
    assert not backend.db.query("SELECT * FROM app_instances")
    assert backend.db.one("SELECT code FROM workflow_app_diagnostics")["code"] == "invalid_app_result"
    assert backend.db.one("SELECT state FROM run_deliveries")["state"] == "delivered"


async def test_run_owner_not_observer_and_explicit_revision_are_authoritative(backend):
    publish()
    record(backend)
    publish(expected_revision=1, title="Revision 2")
    token = current_principal.set(Principal("observer", "admin"))
    try:
        await runs.complete_run(
            "report", "report-1", "completed", result(ui={"app": "daily-report", "revision": 1})
        )
    finally:
        current_principal.reset(token)
    instance = backend.db.one("SELECT * FROM app_instances")
    assert instance["owner_user_id"] == "test-admin"
    assert instance["revision"] == 1
    assert backend.db.one("SELECT owner_user_id FROM chats")["owner_user_id"] == "test-admin"


@pytest.mark.parametrize("status,notify", [("completed", False), ("failed", True)])
async def test_silent_and_failed_runs_do_not_resolve_ui(backend, status, notify):
    publish()
    record(backend)
    value = result(notify=False)
    run_delivery.finish("report-1", status, value, notify=notify)
    await runs.retry_deliveries()
    assert not backend.db.query("SELECT * FROM app_instances")
    assert not backend.db.query("SELECT * FROM workflow_app_diagnostics")
    assert backend.db.one("SELECT state FROM run_deliveries")["state"] == (
        "skipped" if not notify else "delivered"
    )


async def test_delivery_transaction_rollback_retries_without_duplicating_snapshot(backend, monkeypatch):
    publish()
    record(backend)
    run_delivery.finish("report-1", "completed", result(), notify=True)
    original = local_apps.attach_workflow

    def fail_after_attach(*args):
        original(*args)
        raise RuntimeError("delivery unavailable")

    monkeypatch.setattr(local_apps, "attach_workflow", fail_after_attach)
    await runs.try_delivery("report", "report-1", "completed", result())
    assert not backend.db.list_chats()
    assert backend.db.one("SELECT chat_id FROM app_instances")["chat_id"] is None
    assert backend.db.one("SELECT state FROM run_deliveries")["state"] == "pending"
    monkeypatch.setattr(local_apps, "attach_workflow", original)
    backend.db.execute("UPDATE run_deliveries SET retry_at=0")
    await runs.retry_deliveries()
    assert len(backend.db.query("SELECT * FROM app_instances")) == 1
    assert len(backend.db.query("SELECT * FROM messages")) == 1
    chat = backend.db.list_chats()[0]
    backend.db.execute("DELETE FROM chats WHERE id=?", (chat["id"],))
    await runs.deliver_to_chat("report", "report-1", "completed", result())
    assert not backend.db.list_chats()
    assert not backend.db.query("SELECT * FROM app_instances")


async def test_live_delegated_actor_and_enabled_tool_selection(backend):
    actor = access.delegate(
        Principal("test-admin", "admin", "test-session"), tools=["backend_publish_app", "backend_list_apps"]
    )
    try:
        published = await main.backend_mcp.call("publish_app", {"body": payload(), "__denkzeug_actor": actor})
        assert not published.is_error
        listed = await main.backend_mcp.call("list_apps", {"__denkzeug_actor": actor})
        assert not listed.is_error
        denied = await main.backend_mcp.call("get_app", {"app_id": "daily-report", "__denkzeug_actor": actor})
        assert denied.is_error  # Existing per-turn tool selection is not bypassed.
    finally:
        access.release_delegation(actor)
    expired = await main.backend_mcp.call("list_apps", {"__denkzeug_actor": actor})
    assert expired.is_error


def test_scoped_api_keys_and_transport_only_service_are_denied_by_default(client):
    created = client.post(
        "/api/tokens",
        json={
            "name": "Private app reader",
            "grants": [{"scope": "apps:read", "resources": None}],
        },
    )
    assert created.status_code == 201
    headers = {"Authorization": f"Bearer {created.json()['secret']}"}
    assert client.get("/api/apps", headers=headers).status_code == 200
    assert client.post("/api/apps", json=payload(), headers=headers).status_code == 403
    headers = {"Authorization": f"Bearer {INTERNAL_TOKEN}", "X-Internal-Token": INTERNAL_TOKEN}
    assert client.get("/api/apps", headers=headers).status_code == 403
    assert client.post("/api/apps", json=payload(), headers=headers).status_code == 403


async def test_member_authorization_and_no_ambient_admin_read(backend):
    from starlette.requests import Request

    who = Principal("member", "member")
    token = current_principal.set(who)
    try:
        await access.authorize_request(
            Request(
                {
                    "type": "http",
                    "path": "/api/apps",
                    "method": "POST",
                    "headers": [],
                }
            ),
            who,
        )
        assert publish()["app"]["id"] == "daily-report"
    finally:
        current_principal.reset(token)
    assert local_apps.list_apps() == {"apps": []}


def test_hostile_schema_is_killable_and_does_not_hold_database_transaction(backend, monkeypatch):
    schema = {"type": "object", "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}}}
    original = local_apps.validate_data
    lock_states = []

    def checked(schema, data):
        lock_states.append(backend.db._conn.in_transaction)
        return original(schema, data)

    monkeypatch.setattr(local_apps, "validate_data", checked)
    publish(input_schema=schema, sample_data={"value": "a"})
    record(backend)
    started = time.monotonic()
    run_delivery.finish("report-1", "completed", result(data={"value": "a" * 40 + "!"}), notify=True)
    assert time.monotonic() - started < 5
    assert lock_states == [False, False]
    assert backend.db.one("SELECT code FROM workflow_app_diagnostics")["code"] == "invalid_app_result"
    assert backend.db.one("SELECT state FROM run_deliveries")["state"] == "pending"


def test_policy_manifest_and_backend_mcp_exposure():
    validate_routes(main.app)
    names = {"list_apps", "publish_app", "get_app", "get_app_revision", "archive_app"}
    assert names <= set(main.backend_mcp.operations)
    assert POLICIES["POST /api/apps"].scope == "apps:write"
    assert POLICIES["GET /api/apps"].scope == "apps:read"
    # No special privileged service transport: the usual actor delegation and
    # explicit enabled-tool admission apply to these auto-generated tools.
    assert all(
        POLICIES[f"{op['method']} {op['path']}"].authentication == "account"
        for name, op in main.backend_mcp.operations.items()
        if name in names
    )
    assert json.dumps(main.app.openapi())
