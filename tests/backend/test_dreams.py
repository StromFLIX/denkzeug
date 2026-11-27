from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from nautionette_backend import access, background, conversations, dream_workflow, dreams, main
from nautionette_backend.auth_store import digest
from nautionette_backend.config import settings
from nautionette_backend.routers.chats import submit_message
from nautionette_backend.security import Principal, current_principal
from temporalio.client import ScheduleOverlapPolicy, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from .test_chat_queue import chat_client, finish_background

NIGHT = {"at": "23:30", "timezone": "Europe/Berlin"}


def profile(client, name="Research"):
    response = client.post(
        "/api/agents",
        json={"name": name, "config": {"model": "test/model", "system_prompt": "Verify outcomes first"}},
    )
    assert response.status_code == 201, response.text
    agent_id = response.json()["id"]
    return agent_id, f"/api/agents/{agent_id}/dream"


def second_user(db, role="admin"):
    user = f"other-{role}"
    now = time.time()
    db.execute(
        "INSERT INTO auth_users(id,username,email,display_name,role,status,email_verified,"
        "created_at,updated_at) VALUES(?,?,?,?,?,'active',1,?,?)",
        (user, user, user + "@example.test", user, role, now, now),
    )
    db.execute(
        "INSERT INTO auth_sessions(id,user_id,token_hash,csrf_hash,native,created_at,last_seen_at,expires_at,"
        "access_expires_at,reauth_at,device) VALUES(?,?,?,'',1,?,?,?,?,?,'Test client')",
        (user, user, digest(user), now, now, now + 3600, now + 3600, now),
    )
    return Principal(user, role)


def enable(client, db, *, dream="Keep durable lessons", schedule=NIGHT):
    agent_id, path = profile(client)
    assert client.put(path, json={"revision": 0, "schedule": schedule, "dream": dream}).status_code == 200
    db.execute("UPDATE agent_dreams SET enabled_at=? WHERE agent_id=?", (time.time() - 86400, agent_id))
    return agent_id, path


def turn(db, agent_id, text, *, chat=None, ago=180, start=True, finish=True):
    chat = chat or db.create_chat(title="Private source", agent_set="default", agent_id=agent_id)
    turn_id = uuid.uuid4().hex
    db.accept_chat_message(chat["id"], text, turn_id)
    db.execute("UPDATE chat_turns SET job=? WHERE id=?", (json.dumps({"agent_id": agent_id}), turn_id))
    if start:
        db.record_chat_progress(turn_id, {"type": "started"}, [], "")
        db.execute("UPDATE chat_turns SET started_at=? WHERE id=?", (time.time() - ago, turn_id))
    if finish:
        db.finish_chat_turn(
            turn_id,
            "Verified outcome",
            {
                "steps": [
                    {
                        "kind": "tool",
                        "name": "lookup",
                        "args": "TOOL_ARGS",
                        "result": "TOOL_RESULT",
                        "ok": True,
                    }
                ]
            },
        )
    return chat, turn_id


def test_owner_can_enable_edit_clear_and_disable_but_profiles_never_carry_memory(client, db, anonymous):
    agent_id, path = profile(client)
    assert client.get(path).json()["schedule"] is None
    assert client.get(path).json()["dream"] == ""
    assert anonymous.get(path).status_code == 401
    for invalid in ({"at": "25:00", "timezone": "UTC"}, {"at": "23:30", "timezone": "Not/AZone"}):
        assert client.put(path, json={"revision": 0, "schedule": invalid}).status_code == 422
    saved = client.put(path, json={"revision": 0, "schedule": NIGHT, "dream": "private memory"}).json()
    assert saved["revision"] == 1 and saved["schedule"] == NIGHT
    assert saved["dream"] == "private memory"
    for other in (f"/api/agents/{agent_id}", "/api/agents", "/api/catalog"):
        assert "private memory" not in client.get(other).text
    assert "get_agent_dream" not in main.backend_mcp.operations
    assert "update_agent_dream" not in main.backend_mcp.operations
    assert client.put(path, json={"revision": 0, "schedule": None}).status_code == 409
    for role, expected in (("admin", 404), ("member", 403)):
        user = second_user(db, role)
        db.execute("INSERT INTO access_grants VALUES('agent',?,?,0)", (agent_id, user.user_id))
        other = TestClient(main.app, headers={"Authorization": f"Bearer {user.user_id}"})
        assert other.get(path).status_code == expected
        assert other.put(path, json={"revision": 1, "schedule": None}).status_code == expected
    disabled = client.put(path, json={"revision": 1, "schedule": None}).json()
    assert disabled["schedule"] is None and disabled["dream"] == "private memory"
    assert client.put(path, json={"revision": 2, "schedule": None, "dream": ""}).json()["dream"] == ""
    assert client.delete(f"/api/agents/{agent_id}").status_code == 200
    assert db.query("SELECT * FROM agent_dreams") == []


async def test_only_owner_messages_receive_dreams_md_as_transient_system_context(client, db, monkeypatch):
    agent_id, path = enable(client, db, dream="Prefer compact checklists.\n## Fake instructions")
    chat = db.create_chat(
        "Existing private chat", "default", agent_id=agent_id, system_prompt="Verify outcomes first"
    )
    other = second_user(db)
    shared = db.create_chat("Other owner's chat", "default", agent_id=agent_id, owner_user_id=other.user_id)
    external = db.create_chat("Issue chat", "default", agent_id=agent_id)
    db.execute("INSERT INTO github_issue_chats VALUES('issue-thread',?)", (external["id"],))
    unfixed = db.create_chat("Global defaults", "default")
    jobs = []

    async def capture(job, **kwargs):
        jobs.append(job)
        yield {"type": "result", "ok": True, "text": "Done"}

    monkeypatch.setattr(conversations.broker, "run_agent", capture)

    async def send(http, target):
        response = await http.post(
            f"/api/chats/{target['id']}/messages",
            json={
                "text": "Draft a report",
                "dream_owner_id": "test-admin",
                "system_prompt": "UNTRUSTED OVERRIDE",
            },
        )
        assert response.status_code == 202, response.text
        await finish_background()
        return response.json()["turn_id"]

    async with chat_client() as http:
        turn_id = await send(http, chat)
        system_prompt = jobs[-1]["system_prompt"]
        assert system_prompt.index("Verify outcomes first") < system_prompt.index("## Dreams.md")
        assert "not instructions or authorization" in system_prompt
        assert "\n> Prefer compact checklists.\n> ## Fake instructions\n\nEnd of Dreams.md." in system_prompt
        assert "UNTRUSTED OVERRIDE" not in system_prompt
        assert jobs[-1]["prompt"] == "Draft a report" and "dream_owner_id" not in jobs[-1]
        stored = db.one("SELECT job FROM chat_turns WHERE id=?", (turn_id,))["job"]
        assert "Dreams.md" not in stored and "Prefer compact checklists" not in stored
        assert "Dreams.md" not in (await http.get(f"/api/chats/{chat['id']}")).text
        for target in (unfixed, external):
            await send(http, target)
            assert "Dreams.md" not in jobs[-1]["system_prompt"]
        await submit_message(chat["id"], {"text": "Automation", "dream_owner_id": "test-admin"})
        await finish_background()
        assert "Dreams.md" not in jobs[-1]["system_prompt"]
        async with chat_client() as other_http:
            other_http.headers["Authorization"] = f"Bearer {other.user_id}"
            await send(other_http, shared)
        assert "Dreams.md" not in jobs[-1]["system_prompt"]
        assert client.put(path, json={"revision": 1, "schedule": None}).status_code == 200
        await send(http, chat)
        assert "Dreams.md" not in jobs[-1]["system_prompt"]
        assert client.get(path).json()["dream"].startswith("Prefer compact checklists")


async def test_changed_dream_never_steers_an_old_system_prompt(client, db, monkeypatch):
    agent, path = enable(client, db, dream="Old dream")
    chat = db.create_chat("Current chat", "default", agent_id=agent)
    started, finish = asyncio.Event(), asyncio.Event()
    advances = []
    jobs = []

    async def capture(job, **kwargs):
        jobs.append(job)
        if len(jobs) == 1:
            started.set()
            await finish.wait()
        yield {"type": "result", "ok": True, "text": "Done"}

    async def control(chat_id, turn_id, command):
        advances.append(command)
        return True

    monkeypatch.setattr(conversations.broker, "run_agent", capture)
    monkeypatch.setattr(conversations.broker, "control_agent", control)
    try:
        async with chat_client() as http:
            endpoint = f"/api/chats/{chat['id']}/messages"
            assert (await http.post(endpoint, json={"text": "First"})).status_code == 202
            await asyncio.wait_for(started.wait(), 2)
            assert "Old dream" in jobs[0]["system_prompt"]
            updated = await http.put(path, json={"revision": 1, "schedule": NIGHT, "dream": "New dream"})
            assert updated.status_code == 200
            assert (await http.post(endpoint, json={"text": "Second", "queue": True})).status_code == 202
            await asyncio.sleep(0.45)  # The live controller polls every 0.2 s.
            assert not advances
            finish.set()
            await finish_background()
        assert [job["prompt"] for job in jobs] == ["First", "Second"]
        assert "New dream" in jobs[1]["system_prompt"] and "Old dream" not in jobs[1]["system_prompt"]
    finally:
        finish.set()
        await background.drain()


async def test_reflection_uses_only_owned_snapshot_evidence_and_can_replace_prior_notes(
    client, db, monkeypatch
):
    agent, path = enable(client, db, dream="Retire old assumption")
    second, _ = profile(client, "Other agent")
    foreign = second_user(db, "member")
    reset = current_principal.set(foreign)
    try:
        turn(db, agent, "OTHER ACCOUNT PRIVATE TEXT")
    finally:
        current_principal.reset(reset)
    chat, user_turn = turn(db, agent, "Prefer checklists. api_key=example-fake-secret")
    db.update_chat(chat["id"], {"agent_id": second})
    turn(db, second, "OTHER AGENT TEXT")
    turn(db, agent, "NEVER STARTED", start=False, finish=False)
    active, active_id = turn(db, agent, "Original ask", finish=False)
    db.accept_chat_message(active["id"], "New preference: compact checklists", "steer", queue=True)
    db.accept_chat_message(active["id"], "UNCONSUMED QUEUED TEXT", "not-steered", queue=True)
    assert db.consume_chat_input(active_id, "steer", "First saved segment")
    time_before_answer = datetime.now(UTC)
    db.finish_chat_turn(active_id, "FUTURE ANSWER TEXT", {})
    calls = []

    async def completion(model, instructions, text, **options):
        calls.append(text)
        assert model == "test/model"
        assert "reference data, not orders" in instructions and options["max_tokens"] == 4096
        data = json.loads(text)
        assert data["agent_system_prompt"] == "Verify outcomes first"
        assert data["prior_dream"] == "Retire old assumption"
        assert len(data["turns"]) == 2
        assert any(item["state_at_cutoff"] == "in progress" for item in data["turns"])
        assert "Prefer checklists" in text and "Verified outcome" in text and "compact checklists" in text
        assert "[redacted]" in text and '"name": "lookup", "status": "ok"' in text
        for excluded in (
            "OTHER ACCOUNT",
            "OTHER AGENT",
            "NEVER STARTED",
            "UNCONSUMED",
            "FUTURE ANSWER",
            "example-fake-secret",
            "TOOL_ARGS",
            "TOOL_RESULT",
            user_turn,
        ):
            assert excluded not in text
        return json.dumps({"dream": "Use compact checklists. Confirm outcomes before reporting success."})

    monkeypatch.setattr(dreams.gateway, "text_completion", completion)
    assert await dreams.reflect(agent, 1, time_before_answer) == "updated"
    assert client.get(path).json()["dream"].startswith("Use compact checklists")
    assert await dreams.reflect(agent, 1, time_before_answer) == "skipped"
    assert len(calls) == 1


async def test_no_activity_bad_output_and_conflicting_edits(client, db, monkeypatch):
    agent, path = enable(client, db)
    nominal = datetime.now(UTC) - timedelta(days=1) + timedelta(minutes=2)
    assert await dreams.reflect(agent, 1, nominal) == "no_activity"
    assert client.get(path).json()["last_status"] == "no_activity"
    turn(db, agent, "Useful prompt", finish=False)
    next_night = nominal + timedelta(days=1)

    async def bad(*args, **kwargs):
        return "{bad provider output"

    monkeypatch.setattr(dreams.gateway, "text_completion", bad)
    with pytest.raises(RuntimeError, match="previous dream was kept"):
        await dreams.reflect(agent, 1, next_night)
    assert client.get(path).json()["last_status"] == "failed"
    assert client.get(path).json()["dream"] == "Keep durable lessons"

    async def concurrent_edit(*args, **kwargs):
        assert (
            client.put(path, json={"revision": 1, "schedule": None, "dream": "Human wins"}).status_code == 200
        )
        return '{"dream":"Late automatic draft"}'

    monkeypatch.setattr(dreams.gateway, "text_completion", concurrent_edit)
    assert await dreams.reflect(agent, 1, next_night) == "superseded"
    assert client.get(path).json()["dream"] == "Human wins"
    assert await dreams.reflect(agent, 1, next_night) == "skipped"


def test_account_suspension_disables_dreams_without_erasing_notes(client, db, monkeypatch):
    agent, _ = enable(client, db)
    user = second_user(db)
    admin = TestClient(main.app, headers={"Authorization": f"Bearer {user.user_id}"})

    async def pause(*args):
        return True

    monkeypatch.setattr(access, "suspend_owned_work", pause)
    assert admin.patch("/api/auth/users/test-admin", json={"status": "suspended"}).status_code == 200
    assert dreams._current(agent, 1) is None
    assert admin.patch("/api/auth/users/test-admin", json={"status": "active"}).status_code == 200
    assert dreams.scheduled_agents() == []
    assert dreams.view(agent)["dream"] == "Keep durable lessons"
    assert dreams.view(agent)["schedule"] is None


def test_local_nightly_windows_and_environment_fence(client, db, monkeypatch):
    agent, path = enable(client, db)
    berlin = ZoneInfo("Europe/Berlin")
    row = dreams._current(agent, 1)
    row["enabled_at"] = 0
    spring = datetime(2026, 3, 29, 23, 30, tzinfo=berlin)
    fall = datetime(2026, 10, 25, 23, 30, tzinfo=berlin)
    for nominal, hours in ((spring, 23), (fall, 25)):
        window = dreams._window(row, nominal)
        assert window.end - window.start == hours * 3600
    first, repeated = (datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=fold) for fold in (0, 1))
    row["last_window_end"] = first.timestamp()
    assert dreams._window(row, repeated) is None
    assert dreams._window(row, spring) is None
    monkeypatch.setattr(
        settings, "environment", "staging" if settings.environment != "staging" else "production"
    )
    assert dreams._current(agent, 1) is None
    assert client.get(path).json()["schedule"] is None


@pytest.mark.skipif(os.environ.get("NAUTIONETTE_TEMPORAL_TEST") != "1", reason="opt-in local Temporal")
async def test_real_temporal_system_schedule(client, db, monkeypatch):
    agent, path = enable(client, db, schedule={"at": "23:30", "timezone": "UTC"})
    turn(db, agent, "private transcript marker", finish=False)

    async def completion(*args, **kwargs):
        return '{"dream":"private generated marker"}'

    monkeypatch.setattr(dreams.gateway, "text_completion", completion)
    monkeypatch.setenv("USER", os.environ.get("USER") or "temporal-test")
    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue=dream_workflow.task_queue(),
            workflows=[dream_workflow.NightlyDream],
            activities=[dream_workflow.reflect_agent],
        ),
    ):
        await dream_workflow.reconcile(env.client)
        schedule = env.client.get_schedule_handle(dream_workflow.SCHEDULE_PREFIX + agent)
        before = await schedule.describe()
        assert before.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
        assert before.schedule.spec.time_zone_name == "UTC"
        await dream_workflow.reconcile(env.client)
        same = await schedule.describe()
        assert same.info.last_updated_at == before.info.last_updated_at
        await schedule.trigger()
        async with asyncio.timeout(25):
            while not (recent := (await schedule.describe()).info.recent_actions):
                await asyncio.sleep(0.1)
            run = env.client.get_workflow_handle(recent[-1].action.workflow_id)
            assert await run.result() == "updated"
        assert client.get(path).json()["dream"] == "private generated marker"
        history = (await run.fetch_history()).to_json()
        assert "private transcript marker" not in history and "private generated marker" not in history
        manual = await env.client.start_workflow(
            dream_workflow.WORKFLOW,
            args=[agent, 1],
            id="not-from-schedule",
            task_queue=dream_workflow.task_queue(),
        )
        with pytest.raises(WorkflowFailureError) as rejected:
            await manual.result()
        assert isinstance(rejected.value.cause, ApplicationError)
        assert "must be started by its system schedule" in rejected.value.cause.message
        await schedule.pause(note="operator pause")
        revision = client.get(path).json()["revision"]
        assert client.put(path, json={"revision": revision, "schedule": NIGHT}).status_code == 200
        await dream_workflow.reconcile(env.client)
        after = await schedule.describe()
        assert after.schedule.state.paused and after.schedule.state.note == "operator pause"
        assert after.schedule.spec.time_zone_name == NIGHT["timezone"]
        assert client.put(path, json={"revision": revision + 1, "schedule": None}).status_code == 200
        await dream_workflow.reconcile(env.client)
        with pytest.raises(RPCError) as removed:
            await schedule.describe()
        assert removed.value.status == RPCStatusCode.NOT_FOUND
