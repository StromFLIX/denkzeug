"""Workflow profile tasks retain chat isolation, provenance and idempotent admission."""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from nautionette_backend import provenance, workflow_agents
from nautionette_backend.background import wait_for_chat_turns
from nautionette_backend.db import Database

SOURCE = {
    "workflow": "maintenance",
    "workflow_id": "maintenance-run",
    "run_id": "run-uuid",
    "activity_id": "3",
}
KEY = hashlib.sha256(b"run-uuid:3").hexdigest()
ORIGIN = provenance.workflow_origin(
    "maintenance", "maintenance-run", relation="call", trigger="schedule", run_id="run-uuid", activity_id="3"
)


@pytest.fixture
def profile(client, db):
    db.record_run("maintenance", "maintenance-run", "run-uuid", "schedule", {})
    return client.post(
        "/api/agents",
        json={
            "name": "Marvin test",
            "config": {"model": "test/saved", "tools": [], "system_prompt": "Persona"},
        },
    ).json()


async def start(profile, prompt="Check"):
    return await workflow_agents.start(KEY, profile["id"], prompt, source=SOURCE)


def test_profile_validation_is_internal_and_does_not_create_chat(client, internal_headers, profile, db):
    url = f"/internal/agent/tasks/{KEY}"
    payload = {"agent_id": profile["id"], "prompt": "Check", "validate_only": True}
    assert client.post(url, json=payload).status_code == 401
    result = client.post(url, headers=internal_headers, json=payload).json()
    assert result["validated"] and result["model"] == "test/saved"
    assert result["agent_id"] == profile["id"]
    assert db.list_chats() == []
    assert client.get(url).status_code == 401


async def test_profile_task_reconnects_without_restarting_completed_turn(client, profile, db, monkeypatch):
    async def accept(chat_id, payload, *, origin):
        message, created = db.accept_chat_message(
            chat_id, payload["text"], payload["message_id"], [], origin=origin
        )
        if created:
            db.execute(
                "UPDATE chat_turns SET job = ? WHERE id = ?",
                (json.dumps({"agent_id": profile["id"], "origin": origin}), message["id"]),
            )
            db.finish_chat_turn(message["id"], '{"summary":"done"}', {})

    monkeypatch.setattr(workflow_agents, "submit_message", accept)
    first = await start(profile, "Update")
    assert await start(profile, "Update") == first and first["ok"]
    messages = db.list_messages(first["chat_id"])
    assert len(messages) == 2
    assert all(message["meta"]["origin"] == ORIGIN for message in messages)
    chat = db.get_chat(first["chat_id"])
    assert chat["agent_id"] == profile["id"]
    assert chat["model"] == "test/saved" and chat["system_prompt"] == "Persona"
    assert chat["tools"] == [] and chat["timeout_exempt"] is True
    with pytest.raises(HTTPException, match="different agent request"):
        await start(profile, "Different")
    client.delete(f"/api/agents/{profile['id']}")
    assert await start(profile, "Update") == first
    db.delete_chat(first["chat_id"])
    with pytest.raises(HTTPException, match="refusing to replay") as deleted:
        await start(profile, "Update")
    assert deleted.value.status_code == 410 and db.list_chats() == []
    with pytest.raises(HTTPException, match="refusing to replay"):
        workflow_agents.status(KEY)


async def test_task_uses_normal_submit_and_preserves_profile_projects(client, profile, db, monkeypatch):
    submit = AsyncMock()
    monkeypatch.setattr(workflow_agents, "submit_message", submit)
    monkeypatch.setattr(workflow_agents, "status", lambda key: {"state": "running"})
    await start(profile)
    chat_id, turn_id = workflow_agents.task_ids(KEY)
    submit.assert_awaited_once_with(chat_id, {"text": "Check", "message_id": turn_id}, origin=ORIGIN)
    assert db.get_chat(chat_id)["agent_id"] == profile["id"]


@pytest.mark.parametrize("generation_fails", [False, True])
async def test_workflow_tasks_generate_a_title_without_blocking_or_replaying(
    profile, db, live, monkeypatch, generation_fails
):
    source = {**SOURCE, "workflow": "coolify_emergency_handler"}
    db.execute(
        "UPDATE runs SET workflow = ? WHERE workflow_id = ?", (source["workflow"], source["workflow_id"])
    )
    prompt = (
        "Review incident under standing instructions.\n" + "policy " * 3000 + "\nDatabase deployment failed"
    )
    release_title = asyncio.Event()

    async def generate(_model, _instructions, _text):
        await release_title.wait()
        if generation_fails:
            raise RuntimeError("Title service unavailable")
        return "Investigate database deployment failure"

    generate_title = AsyncMock(side_effect=generate)
    monkeypatch.setattr(live.gateway, "chat_title", generate_title)
    first = await asyncio.wait_for(
        workflow_agents.start(KEY, profile["id"], prompt, source=source), timeout=2
    )
    chat_id = first["chat_id"]
    title_task = next(task for task in asyncio.all_tasks() if task.get_name() == f"chat-title-{chat_id}")
    try:
        await asyncio.wait_for(wait_for_chat_turns(), timeout=2)
        assert workflow_agents.status(KEY)["ok"] is True
        assert db.get_chat(chat_id)["title"] == "Coolify emergency handler"
        assert not title_task.done()
        assert (await workflow_agents.start(KEY, profile["id"], prompt, source=source))["chat_id"] == chat_id
    finally:
        release_title.set()
        await asyncio.wait_for(title_task, timeout=2)
    expected = "Coolify emergency handler" if generation_fails else "Investigate database deployment failure"
    assert db.get_chat(chat_id)["title"] == expected
    generate_title.assert_awaited_once()
    model, _, text = generate_title.await_args.args
    assert model == "test/saved"
    assert "Coolify emergency handler" in text
    assert "standing instructions" in text
    assert "Database deployment failed" in text
    assert len(text) <= 12000


def test_legacy_workflow_titles_are_repaired_without_renaming_manual_chats(tmp_path):
    path = str(tmp_path / "workflow-titles.db")
    placeholder = "Workflow agent task"
    database = Database(path)
    try:
        for chat_id in ("original", "manual", "unrelated", "pre-provenance", "unsubmitted"):
            database.create_chat(placeholder, "default", chat_id=chat_id, agent_name="Maintainer")
            database.update_chat(chat_id, {"title": placeholder})  # original fixed title at revision 1
            if chat_id != "unsubmitted":
                origin = None if chat_id == "pre-provenance" else ORIGIN
                database.accept_chat_message(chat_id, "Check", f"turn-{chat_id}", [], origin=origin)
            if chat_id != "unrelated":
                database.execute(
                    "INSERT INTO workflow_agent_tasks (key, chat_id, agent_id, prompt_hash) VALUES (?,?,?,?)",
                    (chat_id, chat_id, "agent", "hash"),
                )
        database.update_chat("manual", {"title": placeholder})  # user explicitly kept the same text
    finally:
        database._conn.close()

    for _ in range(2):
        database = Database(path)
        try:
            for chat_id, expected in {
                "original": ("Maintenance", "provisional", 2),
                "manual": (placeholder, "manual", 2),
                "unrelated": (placeholder, "manual", 1),
                "pre-provenance": ("Workflow run · Maintainer", "provisional", 2),
                "unsubmitted": ("New chat", "provisional", 2),
            }.items():
                chat = database.get_chat(chat_id)
                assert (chat["title"], chat["title_state"], chat["title_revision"]) == expected
        finally:
            database._conn.close()


@pytest.mark.parametrize("error,stopped", [("container failed", False), (None, True)])
async def test_completed_chat_with_error_or_stop_is_failure(client, profile, db, monkeypatch, error, stopped):
    async def accept(chat_id, payload, *, origin):
        _, created = db.accept_chat_message(
            chat_id, payload["text"], payload["message_id"], [], origin=origin
        )
        if created:
            db.finish_chat_turn(payload["message_id"], "not done", {"error": error, "stopped": stopped})

    monkeypatch.setattr(workflow_agents, "submit_message", accept)
    assert (await start(profile))["ok"] is False


async def test_unattributed_new_tasks_and_spoofed_identity_fail_closed(profile):
    with pytest.raises(HTTPException, match="require execution provenance"):
        await workflow_agents.start(KEY, profile["id"], "Check")
    with pytest.raises(HTTPException, match="does not match"):
        await workflow_agents.start("f" * 64, profile["id"], "Check", source=SOURCE)
    with pytest.raises(HTTPException, match="recorded execution"):
        await workflow_agents.start(KEY, profile["id"], "Check", source={**SOURCE, "workflow": "other"})


def test_missing_or_invalid_task_fails(client, internal_headers):
    assert client.get(f"/internal/agent/tasks/{KEY}", headers=internal_headers).status_code == 404
    assert client.get("/internal/agent/tasks/invalid", headers=internal_headers).status_code == 422
    assert (
        client.post(
            f"/internal/agent/tasks/{KEY}",
            headers=internal_headers,
            json={"agent_id": "missing", "prompt": "check", "validate_only": True},
        ).status_code
        == 404
    )
