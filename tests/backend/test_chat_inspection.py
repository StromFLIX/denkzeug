import json

import pytest
from nautionette_backend import main
from nautionette_backend.db import db

BASE = "/api/chat-inspection"


@pytest.fixture
def sessions(backend):
    first = db.create_chat(title="Broken response", agent_set="default", model="copilot/example")
    second = db.create_chat(title="Healthy response", agent_set="default", model="other/model")
    for chat, turn_id, failed in [(first, "failed-turn", True), (second, "healthy-turn", False)]:
        db.accept_chat_message(chat["id"], "Please inspect needle", turn_id)
        db.execute(
            "UPDATE chat_turns SET job = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "model": chat["model"],
                        "project_credentials": [{"token": "SECRET-JOB-TOKEN"}],
                        "system_prompt": "PRIVATE-PROMPT",
                    }
                ),
                turn_id,
            ),
        )
        for event in [
            {"type": "tool", "id": "call-1", "name": "read", "args": {"path": "README.md"}},
            {"type": "tool_done", "id": "call-1", "name": "read", "error": False, "result": "x" * 8000},
            {"type": "result", "ok": not failed, "error": "Pi exited (SIGKILL)" if failed else None},
        ]:
            db.record_chat_progress(turn_id, event, [], "")
        db.finish_chat_turn(
            turn_id, "answer " + "y" * 8000, {"error": "Pi exited (SIGKILL)" if failed else None}
        )
    return first["id"], second["id"]


def test_chat_search_and_session_filters_compose(client, sessions):
    first, second = sessions
    chats = client.get(BASE + "/chats", params={"query": "needle", "limit": 1}).json()
    assert len(chats["chats"]) == 1 and chats["next_offset"] == 1
    rest = client.get(BASE + "/chats", params={"query": "needle", "limit": 1, "offset": 1}).json()
    assert rest["next_offset"] is None
    assert {chats["chats"][0]["id"], rest["chats"][0]["id"]} == {first, second}
    filtered = client.get(
        BASE + "/chats", params={"has_error": True, "model": "copilot/example", "tool": "read"}
    ).json()
    assert [chat["id"] for chat in filtered["chats"]] == [first]
    summary = filtered["chats"][0]
    assert summary["message_count"] == 2 and summary["session_count"] == 1
    assert summary["tool_call_count"] == 1 and summary["tools_used"] == [{"name": "read", "calls": 1}]
    assert summary["failed_session_count"] == 1
    assert client.get(BASE + "/chats", params={"tool": "missing"}).json()["chats"] == []
    assert client.get(BASE + "/chats", params={"query": first}).json()["chats"][0]["id"] == first
    listed = client.get(BASE + "/sessions", params={"chat_id": first, "has_error": True}).json()["sessions"]
    assert listed[0]["id"] == "failed-turn" and listed[0]["has_error"]
    assert listed[0]["last_error_seq"] is not None
    assert client.get(BASE + "/sessions", params={"state": "running"}).json()["sessions"] == []
    assert (
        len(client.get(BASE + "/sessions", params={"has_error": False, "tool": "read"}).json()["sessions"])
        == 1
    )
    assert "SECRET-JOB-TOKEN" not in json.dumps(listed)
    assert "PRIVATE-PROMPT" not in json.dumps(listed)
    assert listed[0]["model"] == "copilot/example"
    assert len(client.get(BASE + "/sessions", params={"model": "other/model"}).json()["sessions"]) == 1
    assert client.get(BASE + "/sessions", params={"created_before": 1}).json()["sessions"] == []
    assert client.get(BASE + "/chats", params={"updated_after": 1e12}).json()["chats"] == []
    tools = client.get(BASE + "/tools", params={"chat_id": first, "session_id": "failed-turn"}).json()
    assert tools["tools"] == [{"name": "read", "calls": 1}]
    assert (
        client.get(BASE + "/tools", params={"chat_id": second, "session_id": "failed-turn"}).status_code
        == 404
    )


def test_sessions_expose_runtime_ownership_without_job_data(client, sessions, monkeypatch):
    from nautionette_backend.routers import chat_inspection

    chat = db.create_chat("Abandoned", "default")["id"]
    db.accept_chat_message(chat, "Private prompt", "orphan")
    page = client.get(BASE + "/sessions", params={"chat_id": chat}).json()["sessions"]
    assert page[0]["runtime_owner"] is False
    monkeypatch.setattr(chat_inspection, "turn_has_owner", lambda turn: turn == "orphan")
    page = client.get(BASE + "/sessions", params={"chat_id": chat}).json()["sessions"]
    assert page[0]["runtime_owner"] is True
    assert "Private prompt" not in json.dumps(page)
    completed = client.get(BASE + "/sessions", params={"state": "completed"}).json()["sessions"]
    assert all(row["runtime_owner"] is None for row in completed)


def test_events_are_filtered_bounded_and_cursor_paginated(client, sessions):
    path = BASE + "/sessions/failed-turn/events"
    page = client.get(path, params={"limit": 1}).json()
    assert page["events"][0]["type"] == "tool"
    assert "data" not in page["events"][0]
    following = client.get(
        path, params={"limit": 1, "after_seq": page["next_after_seq"], "include_data": True, "max_chars": 100}
    ).json()
    assert following["events"][0]["type"] == "tool_done"
    assert len(following["events"][0]["data"]["text"]) == 100
    assert following["events"][0]["data"]["truncated"] is True
    continuation = client.get(
        path, params={"event_types": "tool_done", "include_data": True, "max_chars": 100, "data_offset": 100}
    ).json()
    assert continuation["events"][0]["data"]["offset"] == 100
    assert continuation["events"][0]["data"]["next_offset"] == 200
    errors = client.get(path, params={"event_types": "error,result"}).json()["events"]
    assert len(errors) == 1 and errors[0]["error"]["text"] == "Pi exited (SIGKILL)"
    assert client.get(path, params={"tool": "missing"}).json()["events"] == []
    assert len(client.get(path, params={"tool": "read"}).json()["events"]) == 2
    assert len(client.get(path, params={"event_types": "all"}).json()["events"]) == 5


def test_messages_are_filtered_without_unbounded_metadata(client, sessions):
    path = BASE + f"/chats/{sessions[0]}/messages"
    page = client.get(path, params={"role": "assistant", "has_error": True, "max_chars": 20}).json()
    assert len(page["messages"]) == 1
    message = page["messages"][0]
    assert message["content"]["truncated"] and len(message["content"]["text"]) == 20
    assert "meta" not in message
    continuation = client.get(
        path, params={"role": "assistant", "max_chars": 20, "content_offset": 20}
    ).json()
    assert continuation["messages"][0]["content"]["text"] == "y" * 20
    assert continuation["messages"][0]["content"]["next_offset"] == 40
    assert client.get(path, params={"query": "absent"}).json()["messages"] == []
    assert client.get(path, params={"max_chars": 0}).json()["messages"][0]["content"]["text"] == ""


@pytest.mark.parametrize(
    "suffix", ["/chats", "/sessions", "/chats/missing", "/chats/missing/messages", "/sessions/missing/events"]
)
def test_diagnostics_require_authentication(anonymous, suffix):
    assert anonymous.get(BASE + suffix).status_code == 401


@pytest.mark.parametrize(
    "suffix",
    ["/chats/missing", "/chats/missing/messages", "/sessions/missing/events", "/sessions?chat_id=missing"],
)
def test_unknown_ids_are_not_workflow_runs(client, suffix):
    assert client.get(BASE + suffix).status_code == 404


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_invalid_pagination(client, query):
    assert client.get(BASE + "/chats?" + query).status_code == 422


def test_restart_failures_are_found_even_without_a_result_event(client, backend):
    chat = db.create_chat("Interrupted", "default")
    db.accept_chat_message(chat["id"], "Work", "restarted-turn")
    db.finish_chat_turn(
        "restarted-turn", "partial", {"error": "The answer was interrupted by a backend restart."}
    )
    listed = client.get(BASE + "/sessions", params={"has_error": True}).json()["sessions"]
    assert listed[0]["id"] == "restarted-turn"
    event = client.get(BASE + "/sessions/restarted-turn/events").json()["events"][0]
    assert event["type"] == "done" and "backend restart" in event["error"]["text"]


def test_payload_budget_keeps_a_cursor_for_remaining_events(client, sessions):
    for index in range(30):
        db.record_chat_progress(
            "failed-turn", {"type": "error", "message": "z" * 8000, "index": index}, [], ""
        )
    page = client.get(
        BASE + "/sessions/failed-turn/events",
        params={"event_types": "error", "limit": 100, "max_chars": 4000, "include_data": True},
    ).json()
    assert len(page["events"]) == 5 and page["next_after_seq"] is not None
    assert len(json.dumps(page)) < 45000
    following = client.get(
        BASE + "/sessions/failed-turn/events",
        params={"event_types": "error", "after_seq": page["next_after_seq"], "limit": 100, "max_chars": 4000},
    ).json()
    assert following["events"][0]["seq"] > page["events"][-1]["seq"]


@pytest.mark.parametrize("operation", ["inspect", "search", "sessions"])
@pytest.mark.parametrize("history_turn", ["failed-turn", "healthy-turn"])
@pytest.mark.parametrize("event_type", ["delta", "error"])
def test_chat_diagnostics_do_not_scan_streaming_history(
    client, sessions, operation, history_turn, event_type
):
    first, _ = sessions
    # Summaries must not parse streaming payloads or scan every failure just to
    # detect a failed turn / find its latest error, even within the selected chat.
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO chat_turn_events(turn_id, payload) VALUES (?, ?)",
            [(history_turn, json.dumps({"type": event_type, "text": "x" * 512}))] * 20000,
        )

    statements = []
    instructions = 0

    def budget():
        nonlocal instructions
        instructions += 1000
        return instructions > 100000

    # A VM instruction budget is deterministic on slow CI hosts, unlike a wall
    # clock assertion. The old full-table scan exhausts it on the first summary.
    db._conn.set_progress_handler(budget, 1000)
    db._conn.set_trace_callback(statements.append)
    try:
        path, params = {
            "inspect": (f"/chats/{first}", {}),
            "search": ("/chats", {"query": first}),
            "sessions": ("/sessions", {"chat_id": first}),
        }[operation]
        response = client.get(BASE + path, params=params)
        assert response.status_code == 200
        result = response.json()
        if operation != "inspect":
            result = result["chats" if operation == "search" else "sessions"][0]
        assert result["tool_call_count"] == 1
    finally:
        db._conn.set_progress_handler(None, 0)
        db._conn.set_trace_callback(None)
    event_queries = [sql for sql in statements if "chat_turn_events e" in sql]
    assert len(event_queries) == 3
    for sql in event_queries:
        plan = db.query("EXPLAIN QUERY PLAN " + sql)
        assert not any("SCAN e" in row["detail"] for row in plan), plan
        if operation != "sessions":
            assert any("chat_turn_chat" in row["detail"] for row in plan), plan
        assert any("chat_turn_event_" in row["detail"] for row in plan), plan


@pytest.mark.parametrize("endpoint,key", [("chats", "chats"), ("sessions", "sessions")])
def test_page_enrichment_has_constant_query_count(client, backend, endpoint, key):
    for number in range(51):
        chat = db.create_chat(f"Page {number}", "default")
        turn_id = f"page-turn-{number}"
        db.accept_chat_message(chat["id"], "Read", turn_id)
        db.record_chat_progress(turn_id, {"type": "tool", "name": "read"}, [], "")
        db.finish_chat_turn(turn_id, "Done", {})
    counts = []
    for limit in (1, 20, 50):
        statements = []
        db._conn.set_trace_callback(statements.append)
        try:
            response = client.get(BASE + f"/{endpoint}", params={"limit": limit})
        finally:
            db._conn.set_trace_callback(None)
        assert response.status_code == 200
        page = response.json()
        assert len(page[key]) == limit and page["next_offset"] == limit
        assert all(row["tool_call_count"] == 1 for row in page[key])
        selects = [sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
        counts.append(len(selects))
        assert len([sql for sql in selects if "chat_turn_events e" in sql]) == 3
        if endpoint == "chats":
            page_sql = next(sql for sql in selects if "FROM chats c WHERE" in sql)
            plan = db.query("EXPLAIN QUERY PLAN " + page_sql)
            assert any("chats_owner_updated" in row["detail"] for row in plan), plan
            assert not any("TEMP B-TREE" in row["detail"] for row in plan), plan
        else:
            page_sql = next(sql for sql in selects if sql.startswith("WITH page"))
            plan = db.query("EXPLAIN QUERY PLAN " + page_sql)
            assert any("MATERIALIZE page" in row["detail"] for row in plan), plan
    assert counts[0] == counts[1] == counts[2], counts


def test_batched_summaries_keep_exact_counts_ranking_and_empty_chats(client, sessions):
    first, second = sessions
    empty = db.create_chat("Empty", "default")["id"]
    db.update_chat(first, {"project_ids": ["project-1"], "timeout_exempt": True})
    for number in range(22):
        for _ in range(2 if number == 21 else 1):
            db.record_chat_progress("failed-turn", {"type": "tool", "name": f"tool-{number:02}"}, [], "")
    db.record_chat_progress("failed-turn", {"type": "error", "message": "Another failure"}, [], "")
    db.record_chat_progress("healthy-turn", {"type": "tool_done", "name": "read", "error": True}, [], "")
    page = client.get(BASE + "/chats", params={"limit": 50}).json()
    chats = {chat["id"]: chat for chat in page["chats"]}
    assert chats[first]["tool_call_count"] == 24
    assert chats[first]["distinct_tool_count"] == 23
    assert chats[first]["tools_used_truncated"] is True
    assert len(chats[first]["tools_used"]) == 20
    assert chats[first]["tools_used"][:3] == [
        {"name": "tool-21", "calls": 2},
        {"name": "read", "calls": 1},
        {"name": "tool-00", "calls": 1},
    ]
    assert chats[first]["failed_session_count"] == 1
    assert chats[first]["message_count"] == 2
    assert chats[first]["session_count"] == 1
    assert chats[first]["session_states"] == [{"state": "completed", "count": 1}]
    assert chats[first]["project_ids"] == ["project-1"]
    assert chats[first]["timeout_exempt"] is True
    assert chats[second]["tool_call_count"] == 1
    assert chats[second]["tool_error_count"] == 1
    assert chats[second]["failed_session_count"] == 0
    assert chats[empty]["message_count"] == 0
    assert chats[empty]["session_count"] == 0
    assert chats[empty]["session_states"] == []
    assert chats[empty]["failed_session_count"] == 0
    assert chats[empty]["tool_call_count"] == 0
    assert chats[empty]["tools_used"] == []
    assert chats[empty]["tool_error_count"] == 0
    assert "SECRET-JOB-TOKEN" not in json.dumps(page)
    listed = client.get(BASE + "/sessions", params={"limit": 50}).json()["sessions"]
    turns = {row["id"]: row for row in listed}
    assert turns["failed-turn"]["tool_call_count"] == 24
    assert turns["failed-turn"]["tools_used"] == chats[first]["tools_used"]
    assert turns["failed-turn"]["has_error"] is True
    latest_error = db.one("SELECT MAX(seq) AS seq FROM chat_turn_events WHERE turn_id=?", ("failed-turn",))
    assert turns["failed-turn"]["last_error_seq"] == latest_error["seq"]
    assert turns["healthy-turn"]["has_error"] is False
    assert turns["healthy-turn"]["last_error_seq"] is None
    assert turns["healthy-turn"]["tool_error_count"] == 1


async def test_mcp_chat_diagnostics_use_real_filtered_api(backend, sessions):
    result = await main.backend_mcp.call("search_chats", {"has_error": True, "limit": 1})
    assert not result.is_error
    chat = json.loads(result.content[0].text)["chats"][0]
    assert chat["id"] == sessions[0]
    result = await main.backend_mcp.call("list_chat_sessions", {"chat_id": chat["id"]})
    session = json.loads(result.content[0].text)["sessions"][0]
    result = await main.backend_mcp.call(
        "inspect_chat_session_events", {"session_id": session["id"], "event_types": "result"}
    )
    assert not result.is_error
    assert json.loads(result.content[0].text)["events"][0]["ok"] is False
