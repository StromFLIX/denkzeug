"""Usage facts, privacy, time windows, exports and indexed historical reads."""

import csv
import io
import json
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from nautionette_backend import main, run_statistics, usage
from nautionette_backend.auth_store import digest
from nautionette_backend.security import Principal

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp()
PARAMS = {"start": "2026-11-01", "end": "2026-11-30", "timezone": "UTC"}
COUNTS = {"input": 10, "output": 3, "cacheRead": 2, "cacheWrite": 1}


def turn(
    db,
    *,
    id="t1",
    user="test-admin",
    model="provider/m1",
    agent="a1",
    origin=None,
    state="completed",
    created_at=NOW,
    events=None,
):
    db.execute(
        "INSERT INTO chats(id,title,created_at,updated_at,owner_user_id) VALUES(?,?,?,?,?)",
        (id, "PRIVATE TITLE", created_at, created_at, user),
    )
    db.execute(
        "INSERT INTO messages(id,chat_id,role,content,created_at) VALUES(?,?,?,?,?)",
        (id + "-message", id, "user", "PRIVATE PROMPT", created_at),
    )
    job = {
        "model": model,
        "agent_id": agent,
        "agent_name": "Agent One" if agent else None,
        "agent_set": "default",
        "origin": origin,
        "credential": "PRIVATE CREDENTIAL",
    }
    db.execute(
        "INSERT INTO chat_turns(id,chat_id,user_id,state,job) VALUES(?,?,?,?,?)",
        (id, id, id + "-message", state, json.dumps(job)),
    )
    for event in (
        events
        if events is not None
        else [
            {"type": "usage", "usage": COUNTS},
            {"type": "result", "usage": COUNTS, "text": "PRIVATE REPLY"},
        ]
    ):
        db.execute("INSERT INTO chat_turn_events(turn_id,payload) VALUES(?,?)", (id, json.dumps(event)))
    return id


def report(**extra):
    return usage.report(**PARAMS, who=Principal("test-admin", "admin"), **extra)


def member(db):
    db.execute(
        "INSERT INTO auth_users(id,username,display_name,role,created_at,updated_at) "
        "VALUES('member','member','Member','member',?,?)",
        (NOW, NOW),
    )


def test_sums_response_loops_not_final_result_and_cache_is_disjoint(db):
    turn(db, events=[{"type": "usage", "usage": COUNTS}] * 3 + [{"type": "result", "usage": COUNTS}])
    data = report()
    assert data["summary"] == {
        "tokens": {"input": 30, "output": 9, "cacheRead": 6, "cacheWrite": 3, "total": 48},
        "executions": 1,
        "responses": 3,
        "reports": 3,
    }
    assert data["rows"][0]["tokens"]["total"] == 48
    assert sum((d["tokens"] or {}).get("total", 0) for d in data["daily"]) == 48
    assert len(data["daily"]) == 30
    assert not any(word in json.dumps(data) for word in ("PRIVATE", "credential", "prompt"))


def test_legacy_context_and_result_fallback_are_not_zero_or_double_counted(db):
    context = {
        "source": "provider",
        "input_tokens": 13,
        "output_tokens": 3,
        "cache_read_tokens": 2,
        "cache_write_tokens": 1,
    }
    turn(db, events=[{"type": "usage", "context": context}, {"type": "result", "usage": COUNTS}])
    turn(db, id="fallback", events=[{"type": "result", "usage": COUNTS}, {"type": "result", "usage": COUNTS}])
    assert report()["summary"]["tokens"]["total"] == 32
    assert report()["summary"]["responses"] == 1


@pytest.mark.parametrize(
    "counts", [None, {}, dict.fromkeys(COUNTS, 0), {**COUNTS, "input": True}, {**COUNTS, "input": -1}]
)
def test_missing_or_invalid_usage_stays_unknown(db, counts):
    turn(db, events=[{"type": "usage", "usage": counts}])
    data = report()
    assert data["summary"]["tokens"] is None
    assert data["summary"]["responses"] == 1
    assert data["rows"][0]["tokens"] is None
    assert data["summary"]["executions"] == 1


def test_models_can_change_within_native_session_without_misattribution(db):
    turn(
        db,
        events=[
            {"type": "usage", "usage": COUNTS, "context": {"model": model}}
            for model in ["p/first", "p/second", "p/second"]
        ],
    )
    data = report()
    assert data["summary"]["executions"] == 1
    assert [(r["id"], r["tokens"]["total"]) for r in data["rows"]] == [("p/second", 32), ("p/first", 16)]
    assert report(model="p/first")["summary"]["tokens"]["total"] == 16
    assert report(group="executions")["count"] == 1
    assert report(group="executions")["rows"][0]["model"] == "p/first, p/second"


def test_saved_workflow_tasks_delegates_and_legacy_attempts_are_counted_once(db):
    origin = {"kind": "workflow", "workflow": "digest", "workflow_id": "wf", "trigger": "schedule"}
    turn(db, origin=origin)
    turn(db, id="child", origin={"kind": "agent", "root": origin})
    turn(db, id="plain")
    run = db.record_run("digest", "wf", "run", "schedule", {})
    db.execute("UPDATE runs SET created_at=? WHERE id=?", (NOW, run["id"]))
    stats = run_statistics.AgentStatistics()
    stats.observe({"type": "usage", "usage": COUNTS})
    for call in ["attempt1", "attempt2"]:
        run_statistics.record_call(
            "wf", call, "agent", "research", "failed", model="p/legacy", statistics=stats.snapshot()
        )
    run_statistics.record_call("wf", "mcp1", "mcp", "http", "completed")
    all_data = report()
    assert all_data["summary"]["tokens"]["total"] == 80
    assert all_data["summary"]["executions"] == 5
    flows = report(group="workflows")
    assert flows["rows"][0]["tokens"]["total"] == 64
    assert report(workflow="digest")["summary"]["tokens"]["total"] == 64
    assert report(workload="chats")["summary"]["tokens"]["total"] == 16
    assert report(agent="__none__")["summary"]["tokens"]["total"] == 32
    runs = report(group="runs", workflow="digest")
    assert runs["count"] == 1
    assert runs["rows"][0]["id"] == "wf"
    assert runs["rows"][0]["tokens"]["total"] == 64
    assert runs["rows"][0]["executions"] == 4
    assert runs["rows"][0]["href"] == "/runs/wf"
    day = next(d for d in all_data["daily"] if d["tokens"])
    assert day["chats"]["total"] + day["workflows"]["total"] == day["tokens"]["total"]


def test_nested_root_outside_selected_range_still_attributes_child(db):
    root = turn(
        db,
        id="root",
        created_at=NOW - 45 * 86400,
        origin={"kind": "workflow", "workflow": "digest", "workflow_id": "wf"},
    )
    child = turn(db, id="child")
    db.execute(
        "INSERT INTO agent_calls(id,request_id,parent_turn_id,root_turn_id,parent_chat_id,child_turn_id,"
        "target_agent_id,depth,limits,request_hash) VALUES('c','req',?,?,?,?,'a1',1,'{}','hash')",
        (root, root, root, child),
    )
    assert report(group="workflows")["rows"][0]["tokens"]["total"] == 16


def test_member_scoping_admin_facets_and_links_do_not_expose_private_content(db):
    member(db)
    turn(db)
    turn(db, id="other", user="member", model="p/private", agent="a2")
    who = Principal("member", "member")
    data = usage.report(**PARAMS, who=who)
    assert data["summary"]["tokens"]["total"] == 16
    assert data["facets"]["models"] == [{"id": "p/private", "label": "p/private"}]
    assert data["facets"]["users"] == []
    with pytest.raises(Exception) as denied:
        usage.report(**PARAMS, who=who, user="test-admin")
    assert denied.value.status_code == 403
    with pytest.raises(Exception) as denied:
        usage.report(**PARAMS, who=who, group="users")
    assert denied.value.status_code == 403
    rows = report(group="executions")["rows"]
    assert next(r for r in rows if r["user"] == "member")["href"] is None
    assert next(r for r in rows if r["user"] == "test-admin")["href"] == "/chats/t1"
    assert report(user="member")["summary"]["tokens"]["total"] == 16
    assert len(report(user="member")["facets"]["users"]) == 2


def test_deleted_chats_and_branch_transcript_copies_do_not_duplicate_usage(db):
    turn(db)
    turn(db, id="copy", events=[])
    assert report()["summary"]["tokens"]["total"] == 16
    db.execute("DELETE FROM chats WHERE id='t1'")
    assert report()["summary"]["tokens"] is None


def test_queued_and_steered_inputs_are_not_executions_failed_live_calls_are(db):
    for state in ["queued", "steered", "running", "completed"]:
        turn(db, id=state, state=state)
    assert report()["summary"]["executions"] == 2
    assert report()["summary"]["tokens"]["total"] == 32


def test_local_days_dst_half_open_bounds_and_start_attribution(db):
    # DST fall-back in New York, a 25-hour local day.
    lower = datetime(2026, 11, 1, 4, tzinfo=UTC).timestamp()
    for i, created in enumerate([lower - 1, lower, lower + 25 * 3600 - 1, lower + 25 * 3600]):
        turn(db, id=str(i), created_at=created)
    data = usage.report(
        start="2026-11-01",
        end="2026-11-01",
        timezone="America/New_York",
        who=Principal("test-admin", "admin"),
    )
    assert data["summary"]["tokens"]["total"] == 32
    assert len(data["daily"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"start": "bad"},
        {"start": "2025-01-01"},
        {"end": "2026-10-01"},
        {"timezone": "bad/zone"},
        {"group": "bad"},
        {"workload": "bad"},
        {"sort": "bad"},
    ],
)
def test_invalid_selections_are_rejected(db, change):
    with pytest.raises(Exception) as invalid:
        usage.report(**{**PARAMS, **change}, who=Principal("test-admin", "admin"))
    assert invalid.value.status_code == 422


def test_pagination_export_and_csv_formula_safety(db):
    for i in range(4):
        turn(db, id=str(i), model=f"p/{i}")
    data = report(offset=1, limit=2)
    assert data["count"] == 4
    assert len(data["rows"]) == 2
    assert data["summary"]["tokens"]["total"] == 64
    turn(db, id="formula", model="=PRIVATE()")
    csv_data = usage.export_csv(report())
    rows = list(csv.DictReader(io.StringIO(csv_data)))
    assert len(rows) == 5
    assert next(r for r in rows if "PRIVATE" in r["label"])["label"] == "'=PRIVATE()"
    assert sum(int(r["total"]) for r in rows) == 80


def test_api_session_policy_validation_export_and_no_external_requests(client, db):
    turn(db)
    params = PARAMS
    response = client.get("/api/settings/usage", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["tokens"]["total"] == 16
    for extra in [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"timezone": "bad"}]:
        assert client.get("/api/settings/usage", params={**params, **extra}).status_code == 422
    exported = client.get("/api/settings/usage/export", params=params)
    assert exported.status_code == 200
    assert exported.headers["cache-control"] == "no-store"
    assert "text/csv" in exported.headers["content-type"]
    assert "16" in exported.text
    assert client.get("/api/settings/usage", headers={"Authorization": ""}).status_code == 401


def test_usage_read_indexes_exist_and_date_queries_are_indexed(db):
    indexes = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"messages_created", "chat_turn_usage", "runs_created"} <= indexes
    plan = db.query(
        "EXPLAIN QUERY PLAN SELECT id FROM messages WHERE created_at >= ? AND created_at < ?", (NOW, NOW + 1)
    )
    assert any("messages_created" in row["detail"] for row in plan)
    plan = db.query(
        "EXPLAIN QUERY PLAN SELECT seq FROM chat_turn_events WHERE turn_id=? "
        "AND json_extract(payload, '$.type') IN ('usage','result')",
        ("turn",),
    )
    assert any("chat_turn_usage" in row["detail"] for row in plan)


def test_http_member_totals_facets_export_and_cross_account_denial(client, db):
    member(db)
    now = time.time()
    db.execute(
        "INSERT INTO auth_sessions(id,user_id,token_hash,csrf_hash,native,created_at,last_seen_at,"
        "expires_at,access_expires_at,reauth_at,device) "
        "VALUES('member-session','member',?,'',1,?,?,?,?,?,'Test')",
        (digest("member-access"), now, now, now + 86400, now + 86400, now),
    )
    turn(db)
    turn(db, id="member-turn", user="member", model="member/model")
    http = TestClient(main.app, headers={"Authorization": "Bearer member-access"})
    response = http.get("/api/settings/usage", params=PARAMS)
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["tokens"]["total"] == 16
    assert response.json()["facets"]["users"] == []
    for path in ["/api/settings/usage", "/api/settings/usage/export"]:
        assert http.get(path, params={**PARAMS, "user": "test-admin"}).status_code == 403
        assert http.get(path, params={**PARAMS, "group": "users"}).status_code == 403
    exported = http.get("/api/settings/usage/export", params=PARAMS)
    assert exported.status_code == 200
    assert "member/model" in exported.text
    assert "provider/m1" not in exported.text


def test_workspace_usage_is_not_available_to_api_keys(client, db):
    created = client.post(
        "/api/tokens",
        json={"name": "Settings reader", "grants": [{"scope": "settings:read", "resources": None}]},
    )
    assert created.status_code == 201, created.text
    http = TestClient(main.app, headers={"Authorization": f"Bearer {created.json()['secret']}"})
    for path in ["/api/settings/usage", "/api/settings/usage/export"]:
        assert http.get(path, params=PARAMS).status_code == 403


def test_export_is_not_paginated_and_cap_does_not_silently_truncate(client, monkeypatch):
    sample = {"start": "2026-11-01", "end": "2026-11-30", "timezone": "UTC", "rows": [], "count": 10001}
    calls = []

    def large_report(**kwargs):
        calls.append(kwargs)
        return sample

    monkeypatch.setattr(usage, "report", large_report)
    response = client.get("/api/settings/usage/export", params=PARAMS)
    assert response.status_code == 422
    assert calls[0]["limit"] == 10001
