"""Ownership boundaries independent of the authentication credential format.

The fake identity dependency is test-local, not a production APP_TOKEN bypass.
Real session/cookie authentication is covered by the authentication suite.
"""

from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from nautionette_backend import access, chat_drafts, chat_search, security
from nautionette_backend import db as db_module
from nautionette_backend.db import Database
from nautionette_backend.routers import chats


@pytest.fixture
def private_db(tmp_path, monkeypatch):
    context = ContextVar("test_principal", default=None)
    monkeypatch.setattr(security, "current_principal", context, raising=False)
    database = Database(str(tmp_path / "private.sqlite"))
    monkeypatch.setattr(db_module, "db", database)
    monkeypatch.setattr(chats, "db", database)
    monkeypatch.setattr(chat_drafts, "db", database)
    for user, role in (("alice", "admin"), ("bob", "member"), ("carol", "admin")):
        database.execute(
            "INSERT INTO auth_users(id,username,email,display_name,role,status,email_verified,"
            "created_at,updated_at) "
            "VALUES(?,?,?,?,?,'active',1,0,0)",
            (user, user, user + "@example.test", user, role),
        )
    legacy = database.create_chat(title="legacy secret", agent_set="default")
    with database.transaction() as conn:
        access.claim_legacy("alice", conn)
    yield database, context, legacy
    database._conn.close()


def identity(user="alice", role="admin", service=False):
    return SimpleNamespace(user_id=user, role=role, service=service, session_id=None)


def make_chat(database, context, user, role="member", title="private"):
    token = context.set(identity(user, role))
    try:
        return database.create_chat(title=title, agent_set="default")
    finally:
        context.reset(token)


def test_legacy_claim_is_atomic_and_once(private_db):
    database, context, legacy = private_db
    assert database.get_chat(legacy["id"])["owner_user_id"] == "alice"
    access.claim_legacy("bob")
    assert database.get_chat(legacy["id"])["owner_user_id"] == "alice"
    with pytest.raises(Exception, match="owner required"):
        database.create_chat(title="orphan", agent_set="default")
    assert not database.one("SELECT id FROM chats WHERE title='orphan'")
    bob = make_chat(database, context, "bob")
    assert bob["owner_user_id"] == "bob"


def test_admin_never_bypasses_private_ownership(private_db):
    database, context, legacy = private_db
    bob = make_chat(database, context, "bob")
    for who in (identity("alice"), identity("carol"), identity("service", service=True)):
        with pytest.raises(Exception) as caught:
            access.require_chat(bob["id"], who)
        assert caught.value.status_code == 404
    assert access.require_chat(bob["id"], identity("bob", "member"))["id"] == bob["id"]
    with pytest.raises(HTTPException):
        access.require_chat(legacy["id"], identity("carol"))


def test_http_lists_drafts_files_and_stream_probes_are_private(private_db):
    database, context, legacy = private_db
    bob = make_chat(database, context, "bob")
    app = FastAPI()

    async def authenticated(request: Request):
        user = request.headers.get("x-test-user", "bob")
        who = identity(user, "admin" if user in {"alice", "carol"} else "member")
        token = context.set(who)
        try:
            await access.authorize_request(request, who)
            yield who
        finally:
            context.reset(token)

    app.dependency_overrides[security.require_user] = authenticated
    app.include_router(chats.router)
    with TestClient(app) as client:
        response = client.get("/api/chats")
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["chats"]] == [bob["id"]]
        for path in (
            f"/api/chats/{legacy['id']}",
            f"/api/chats/{legacy['id']}/draft",
            f"/api/chats/{legacy['id']}/attachments/unknown",
            f"/api/chats/{legacy['id']}/stream",
        ):
            assert client.get(path).status_code == 404
        assert client.head(f"/api/chats/{legacy['id']}/stream").status_code == 404
        assert client.put(f"/api/chats/{legacy['id']}/draft", json={}).status_code == 404
        assert client.get(f"/api/chats/{bob['id']}/draft").status_code == 200
        assert client.post(f"/api/chats/{bob['id']}/messages", json={"text": "execute"}).status_code == 403
        # A second admin does not get the initial administrator's chats either.
        assert client.get(f"/api/chats/{legacy['id']}", headers={"x-test-user": "carol"}).status_code == 404


@pytest.mark.asyncio
async def test_member_routes_and_grants_fail_closed(private_db):
    database, context, legacy = private_db
    bob = identity("bob", "member")

    async def check(path, method="GET", who=bob):
        request = Request({"type": "http", "path": path, "method": method, "headers": []})
        await access.authorize_request(request, who)

    for path in (
        "/api/settings",
        "/api/system",
        "/api/model-integrations",
        "/api/mcp-servers",
        "/api/miniapps/overview",
        "/api/miniapps/contract",
        "/api/new-unreviewed-route",
    ):
        with pytest.raises(Exception) as caught:
            await check(path)
        assert caught.value.status_code == 403
    with pytest.raises(HTTPException):
        await check("/api/agents/assistant")
    database.execute("INSERT INTO access_grants VALUES('agent','assistant','bob',0)")
    await check("/api/agents/assistant")
    for path in ("/api/workflows/assistant/run", "/api/miniapps/assistant/invoke", "/api/agents/assistant"):
        with pytest.raises(HTTPException):
            await check(path, "POST")
    with pytest.raises(HTTPException):
        access.require_execution(bob)
    with pytest.raises(HTTPException):
        await check("/api/settings", who=identity("service", service=True))
    await check("/api/chat-outputs/read", "POST", identity("service", service=True))
    with pytest.raises(HTTPException):
        await check("/api/chat-outputs/read", "POST")
    database.execute("DELETE FROM access_grants WHERE user_id='bob'")
    assert not access.allowed("agent", "assistant", bob)


@pytest.mark.asyncio
async def test_search_only_counts_and_returns_own_documents(private_db, monkeypatch):
    database, context, legacy = private_db
    bob = make_chat(database, context, "bob", title="private bob searchword")
    database.add_message(legacy["id"], "user", "secret searchword hidden")
    database.add_message(bob["id"], "user", "own searchword visible")
    monkeypatch.setattr(chat_search.settings, "search_semantic_enabled", False)
    search = chat_search.ChatSearch(database)
    token = context.set(identity("bob", "member"))
    try:
        result = await search.search("searchword")
        assert {row["chat_id"] for row in result["results"]} == {bob["id"]}
        assert "secret" not in str(result)
        expected = database.one("SELECT COUNT(*) AS n FROM search_documents WHERE chat_id=?", (bob["id"],))
        assert result["semantic"]["pending"] == expected["n"]
        with pytest.raises(HTTPException):
            access.require_owner_execution("chats", bob["id"])
    finally:
        context.reset(token)


@pytest.mark.asyncio
async def test_runs_background_inheritance_and_output_paths(private_db):
    database, context, legacy = private_db
    bob = make_chat(database, context, "bob")
    token = context.set(identity("bob", "member"))
    try:
        row = database.record_run("job", "bob-run", None, "manual", {})
        assert row["owner_user_id"] == "bob"
    finally:
        context.reset(token)
    database.execute("INSERT INTO access_resources VALUES('workflow','job','alice')")
    background = database.record_run("job", "scheduled-run", None, "schedule", {})
    assert background["owner_user_id"] == "alice"
    assert [row["workflow_id"] for row in database.list_runs(owner_user_id="bob")] == ["bob-run"]
    for who in (identity("alice"), identity("carol"), identity("service", service=True)):
        with pytest.raises(HTTPException) as caught:
            access.require_run("bob-run", who)
        assert caught.value.status_code == 404
    for path in (
        f"/api/chats/{legacy['id']}/outputs",
        f"/api/chats/{legacy['id']}/outputs/output/revisions/1/files/file",
        "/api/runs/scheduled-run/history",
        "/api/runs/scheduled-run/graph",
    ):
        request = Request({"type": "http", "path": path, "method": "GET", "headers": []})
        with pytest.raises(HTTPException) as caught:
            await access.authorize_request(request, identity("bob", "member"))
        assert caught.value.status_code == 404
    request = Request(
        {"type": "http", "path": f"/api/chats/{bob['id']}/outputs", "method": "GET", "headers": []}
    )
    await access.authorize_request(request, identity("bob", "member"))


@pytest.mark.asyncio
async def test_backend_mcp_never_dispatches_member_tools(private_db, monkeypatch):
    from nautionette_backend import backend_mcp, main

    database, context, _ = private_db
    token = context.set(identity("bob", "member"))

    def no_transport(*args, **kwargs):
        raise AssertionError("member tool must not reach a transport")

    monkeypatch.setattr(backend_mcp.httpx, "ASGITransport", no_transport)
    try:
        result = await main.backend_mcp.call("system_status", {})
        assert result.is_error
        assert "live delegated account capability" in result.content[0].text
    finally:
        context.reset(token)
