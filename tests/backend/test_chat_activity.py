"""Agent rail activity comes from durable turns, not a truncated chat list."""

import json
import time

from nautionette.telemetry import SafeSpanExporter
from nautionette_backend import chat_startup, conversations
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def test_chat_startup_exports_stages_once_and_uses_only_bounded_metric_labels(monkeypatch):
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    meter = meter_provider.get_meter("test")
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(SafeSpanExporter(exporter)))
    monkeypatch.setattr(chat_startup, "_tracer", tracer_provider.get_tracer("test"))
    monkeypatch.setattr(chat_startup, "_duration", meter.create_histogram("denkzeug.chat.startup.duration"))
    monkeypatch.setattr(chat_startup, "_turns", meter.create_counter("denkzeug.chat.startup.turns"))
    monotonic = iter(n * 1_000_000 for n in range(10))
    monkeypatch.setattr(chat_startup, "monotonic_ns", lambda: next(monotonic))
    try:
        startup = chat_startup.ChatStartup(
            "chat-id", "turn-id", {"model": "test/model", "project_ids": ["private"], "tools": []}
        )
        for key in ("reconciled", "model_phase", "first_text", "first_text"):
            startup.mark(key)
        startup.finish()
        startup.finish()
        failed = chat_startup.ChatStartup("another-chat", "another-turn", {})
        failed.finish()

        data = reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
        points = {metric.name: metric.data.data_points for metric in data}
        durations = {
            point.attributes["stage"]: (point.sum, point.count)
            for point in points["denkzeug.chat.startup.duration"]
        }
        assert durations == {
            "reconcile": (1, 1),
            "model_phase_to_first_text": (1, 1),
            "time_to_first_text": (3, 1),
        }
        assert all(
            set(point.attributes) == {"project_count", "mcp_selection", "stage"}
            and point.attributes["project_count"] == "one"
            and point.attributes["mcp_selection"] == "empty"
            for point in points["denkzeug.chat.startup.duration"]
        )
        turns = {point.attributes["outcome"]: point.value for point in points["denkzeug.chat.startup.turns"]}
        assert turns == {"first_text": 1, "no_text": 1}
        spans = exporter.get_finished_spans()
        root = next(span for span in spans if span.attributes.get("nautionette.turn_id") == "turn-id")
        assert root.end_time - root.start_time == 3_000_000
        assert root.attributes["gen_ai.request.model"] == "test/model"
        assert {span.name for span in spans if span.parent == root.context} == {
            "chat.startup.reconcile",
            "chat.startup.model_phase_to_first_text",
        }
        assert "private" not in str(data)
    finally:
        meter_provider.shutdown()
        tracer_provider.shutdown()


def test_activity_counts_all_chats_and_only_current_agent_membership(client, db):
    assert client.get("/api/chats").json()["activity"] == {"running": 0, "queued": 0, "agents": {}}
    chats = [
        db.create_chat("Ada one", "default", agent_id="ada"),
        db.create_chat("Ada two", "default", agent_id="ada"),
        db.create_chat("Grace", "default", agent_id="grace"),
        db.create_chat("Custom Ada", "default", agent_id="ada", agent_customized=True),
        db.create_chat("Unassigned", "default"),
    ]
    for index, chat in enumerate(chats):
        db.accept_chat_message(chat["id"], "Run", f"run-{index}")
        db.accept_chat_message(chat["id"], "Wait", f"wait-{index}", queue=True)
    # Counts must include active chats outside the 200-row recent list.
    for index in range(201):
        db.create_chat(f"Newer idle chat {index}", "default")
    response = client.get("/api/chats").json()
    assert len(response["chats"]) == 200
    assert response["agent_usage_7d"] == {"ada": 6, "grace": 2}
    assert not {chat["id"] for chat in chats} & {chat["id"] for chat in response["chats"]}
    assert response["activity"] == {
        "running": 5,
        "queued": 5,
        "agents": {"ada": {"running": 3, "queued": 3}, "grace": {"running": 1, "queued": 1}},
    }
    db.update_chat(chats[0]["id"], {"agent_id": "grace"})
    db.update_chat(chats[1]["id"], {"agent_customized": True})
    activity = client.get("/api/chats").json()["activity"]
    assert activity == {
        "running": 5,
        "queued": 5,
        "agents": {"ada": {"running": 2, "queued": 2}, "grace": {"running": 2, "queued": 2}},
    }


def test_agent_usage_counts_only_recent_direct_user_messages_for_the_account(client, db):
    chat = db.create_chat("Ada", "default", agent_id="ada")
    delegated = db.create_chat("Grace delegate", "default", agent_id="grace")
    other = db.create_chat("Another account", "default", agent_id="ada", owner_user_id="another-user")
    db.execute("UPDATE chats SET delegated_parent_chat_id = ? WHERE id = ?", (chat["id"], delegated["id"]))
    db.accept_chat_message(delegated["id"], "Delegated prompt", "delegated")
    db.accept_chat_message(other["id"], "Other user's prompt", "private")
    db.add_message(chat["id"], "assistant", "Reply")
    db.add_message(chat["id"], "user", "Copied prompt without a turn")
    db.accept_chat_message(chat["id"], "Recent prompt", "recent")
    db.accept_chat_message(chat["id"], "Old prompt", "old", queue=True)
    db.execute("UPDATE messages SET created_at = ? WHERE id = 'old'", (time.time() - 8 * 86400,))
    db.execute("UPDATE chat_turns SET job = ? WHERE id = 'recent'", ('{"agent_id":"ada"}',))
    db.update_chat(chat["id"], {"agent_id": "grace"})

    assert client.get("/api/chats").json()["agent_usage_7d"] == {"ada": 1}
    assert db.recent_agent_usage("another-user") == {"ada": 1}


def test_activity_tracks_steering_pause_resume_discard_and_completion(client, db, monkeypatch):
    events = []
    monkeypatch.setattr(conversations.bus, "publish", lambda kind, data: events.append((kind, data)))
    spawned = []

    def spawn(coroutine, **kwargs):
        spawned.append(kwargs)
        coroutine.close()

    monkeypatch.setattr(conversations, "spawn", spawn)
    chat_id = db.create_chat("Ada", "default", agent_id="ada")["id"]
    db.accept_chat_message(chat_id, "Run", "active")
    for message_id in ("steer", "next", "discard"):
        db.accept_chat_message(chat_id, message_id, message_id, queue=True)
        db.execute("UPDATE chat_turns SET job = '{}' WHERE id = ?", (message_id,))

    def check(running, queued):
        counts = {"running": running, "queued": queued}
        assert client.get("/api/chats").json()["activity"] == {
            **counts,
            "agents": {"ada": counts} if running or queued else {},
        }

    check(1, 3)
    assert db.consume_chat_input("active", "steer")
    check(1, 2)
    assert db.stop_chat_turn(chat_id, "active")
    # Stop requested is still running until the agent actually finishes cleanup.
    check(1, 2)
    db.finish_chat_turn("active", "Stopped", {})
    check(0, 2)
    assert client.delete(f"/api/chats/{chat_id}/queue/discard").status_code == 200
    assert events[-1] == ("chat.queue.changed", {"chat_id": chat_id})
    check(0, 1)
    assert client.post(f"/api/chats/{chat_id}/queue/resume").status_code == 200
    assert spawned and events[-1] == ("chat.queue.changed", {"chat_id": chat_id})
    check(1, 0)
    db.finish_chat_turn("next", "Done", {})
    check(0, 0)
    db.delete_chat(chat_id)
    check(0, 0)


async def test_consumed_input_refreshes_other_clients_without_waiting_for_progress(backend, monkeypatch):
    db = backend.db
    chat_id = db.create_chat("Ada", "default", agent_id="ada")["id"]
    db.accept_chat_message(chat_id, "Run", "active")
    db.accept_chat_message(chat_id, "Steer", "steer", queue=True)
    events = []
    monkeypatch.setattr(conversations.bus, "publish", lambda kind, data: events.append((kind, data)))

    async def agent(job):
        yield {"type": "delta", "text": "Before steering"}
        yield {"type": "input_consumed", "id": "steer"}
        assert db.chat_activity() == {
            "running": 1,
            "queued": 0,
            "agents": {"ada": {"running": 1, "queued": 0}},
        }
        assert ("chat.queue.changed", {"chat_id": chat_id}) in events
        yield {"type": "input_consumed", "id": "steer"}  # duplicate acknowledgement
        assert sum(kind == "chat.queue.changed" for kind, _ in events) == 1
        yield {"type": "result", "ok": True}

    monkeypatch.setattr(conversations, "stream_agent", agent)
    await conversations.run_turn("active", chat_id, {"prompt": "Run"})
    assert db.chat_activity() == {"running": 0, "queued": 0, "agents": {}}
    done = db.one("SELECT payload FROM chat_turn_events WHERE turn_id = 'active' ORDER BY seq DESC")
    startup = json.loads(done["payload"])["message"]["meta"]["startup_ms"]
    assert 0 <= startup["reconciled"] <= startup["input_ready"] <= startup["worktrees_ready"]
    assert startup["worktrees_ready"] <= startup["git_credentials_ready"] <= startup["agent_dispatch"]
    assert startup["agent_dispatch"] <= startup["first_text"]
    assert "first_reasoning" not in startup
