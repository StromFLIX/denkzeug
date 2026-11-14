"""Real sessions through the production router and delegated MCP boundaries."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import local

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from nautionette_backend import account_streams, auth, chat_outputs, main
from nautionette_backend.db import Database
from nautionette_backend.security import Principal, current_principal


@pytest.fixture
def member(client, db):
    row = db.one("SELECT * FROM auth_users WHERE id='test-admin'")
    db.execute(
        "INSERT INTO auth_users(id,username,email,display_name,role,email_verified,password_hash,created_at,"
        "updated_at) VALUES('member','member','member@example.test','Member','member',1,?,?,?)",
        (row["password_hash"], time.time(), time.time()),
    )
    http = TestClient(main.app)
    result = http.post(
        "/api/auth/login",
        json={"username": "member", "password": "a correct horse battery staple for tests", "native": True},
    )
    assert result.status_code == 200
    http.headers["Authorization"] = "Bearer " + result.json()["access_token"]
    return http


def test_member_private_chats_granted_agents_and_admin_separation(client, member, db):
    admin_chat = client.post("/api/chats", json={"title": "Admin secret"}).json()
    assert member.get(f"/api/chats/{admin_chat['id']}").status_code == 404
    agent = client.post("/api/agents", json={"name": "Shared assistant", "config": {"tools": []}}).json()
    assert member.post("/api/chats", json={"agent_id": agent["id"]}).status_code == 404
    assert (
        client.put(
            "/api/access/grants", json={"kind": "agent", "resource_id": agent["id"], "user_id": "member"}
        ).status_code
        == 200
    )
    created = member.post("/api/chats", json={"agent_id": agent["id"], "title": "Member secret"})
    assert created.status_code == 200, created.text
    chat = created.json()
    assert chat["owner_user_id"] == "member"
    assert client.get(f"/api/chats/{chat['id']}").status_code == 404
    assert member.get("/api/chats").json()["chats"][0]["id"] == chat["id"]
    assert member.patch(f"/api/chats/{chat['id']}", json={"tools": None}).status_code == 403
    assert member.post("/api/agents", json={"name": "Escalate", "config": {}}).status_code == 403
    assert member.get("/api/settings").status_code == 403
    assert client.get("/api/chat-inspection/chats").json()["chats"][0]["id"] == admin_chat["id"]
    assert member.get("/api/chat-inspection/chats").json()["chats"][0]["id"] == chat["id"]
    assert member.get("/api/catalog").json()["agents"][0]["id"] == agent["id"]
    assert (
        client.request(
            "DELETE",
            "/api/access/grants",
            json={"kind": "agent", "resource_id": agent["id"], "user_id": "member"},
        ).status_code
        == 200
    )
    assert member.post(f"/api/chats/{chat['id']}/messages", json={"text": "run"}).status_code == 403


async def test_live_turn_delegation_never_becomes_a_master_token(client, member, db):
    admin_chat = db.create_chat("Administrator's chat", "default")
    db.accept_chat_message(admin_chat["id"], "Inspect my workspace", "admin-turn")
    member_chat = db.create_chat("Member's chat", "default", owner_user_id="member")
    db.accept_chat_message(member_chat["id"], "Inspect my workspace", "member-turn")
    db.execute(
        "UPDATE chat_turns SET job=? WHERE id IN ('admin-turn','member-turn')", (json.dumps({"tools": None}),)
    )
    with chat_outputs.publication_grant(admin_chat["id"], "admin-turn") as admin_grant:
        result = await main.backend_mcp.call("search_chats", {"__denkzeug_actor": admin_grant["grant"]})
        assert not result.is_error
        assert member_chat["id"] not in result.content[0].text
    with chat_outputs.publication_grant(member_chat["id"], "member-turn") as member_grant:
        result = await main.backend_mcp.call("system_status", {"__denkzeug_actor": member_grant["grant"]})
        assert result.is_error
        result = await main.backend_mcp.call(
            "inspect_chat", {"chat_id": admin_chat["id"], "__denkzeug_actor": member_grant["grant"]}
        )
        assert result.is_error and admin_chat["title"] not in result.content[0].text
        db.execute("UPDATE auth_users SET status='suspended' WHERE id='member'")
        result = await main.backend_mcp.call("search_chats", {"__denkzeug_actor": member_grant["grant"]})
        assert result.is_error


async def test_stream_revocation_and_event_projection(client, member, db):
    admin_chat = db.create_chat("Administrator's chat", "default")
    member_chat = db.create_chat("Member's chat", "default", owner_user_id="member")
    who = Principal("test-admin", "admin", "test-session")
    assert account_streams.visible({"kind": "chat.changed", "chat_id": admin_chat["id"]}, who)
    assert not account_streams.visible({"kind": "chat.changed", "chat_id": member_chat["id"]}, who)
    assert not account_streams.visible({"kind": "unknown.private", "secret": "hidden"}, who)

    async def frames():
        yield "first"
        db.execute("UPDATE auth_sessions SET revoked_at=? WHERE id='test-session'", (time.time(),))
        yield "must not leak after revocation"

    assert [frame async for frame in account_streams.guarded(frames(), who)] == ["first"]


def test_two_database_connections_cannot_bootstrap_two_initial_admins(db, monkeypatch):
    db.execute("DELETE FROM auth_users")
    db.execute("UPDATE access_state SET legacy_owner=NULL WHERE id=1")
    current_principal.set(None)
    token = auth.issue_setup_token()
    connections = [Database(db.path), Database(db.path)]
    thread_state = local()
    monkeypatch.setattr(auth, "store", lambda: thread_state.database)

    def claim(index):
        thread_state.database = connections[index]
        try:
            auth.bootstrap(
                token, f"owner{index}", f"owner{index}@example.test", "a concurrent bootstrap passphrase 728!"
            )
            return True
        except HTTPException:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(claim, (0, 1))) == 1
        assert db.one("SELECT COUNT(*) AS n FROM auth_users")["n"] == 1
    finally:
        for connection in connections:
            connection._conn.close()
