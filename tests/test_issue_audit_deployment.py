import hashlib
from copy import deepcopy

import pytest

from scripts import check_issue_audit_deployment as audit
from scripts.smoke_api import APIError

CODE = "reviewed staging workflow source"
PINS = audit.Pins("a" * 40, hashlib.sha256(CODE.encode()).hexdigest(), "qa-user", "qa-profile")
BUILD = {"environment": "staging", "build_commit": PINS.build}
RUN = audit.NAME + "-1700000000-012abc"
RUN_PATH = "/api/runs/" + RUN
EXECUTION = "11111111-2222-3333-4444-555555555555"


def history():
    return {
        "workflow_id": RUN,
        "workflow": audit.NAME,
        "trigger": "manual",
        "status": "completed",
        "history_status": "available",
        "events": [
            {"event": "workflow.started", "workflow_type": audit.NAME, "input": PINS.inputs},
            *(
                {
                    "event": "activity.completed",
                    "activity": "save_artifact",
                    "result": {"name": name, "bytes": 5, "path": f"/artifacts/{name}"},
                }
                for name in (
                    f"audit-round-{RUN}-1.json",
                    f"audit-review-{RUN}.json",
                    f"audit-final-{RUN}.json",
                )
            ),
            # A silent audit with no findings is still a real audit if the
            # required save_artifact metadata proves the reviewer was reached.
            {"event": "workflow.completed", "result": {"notify": False, "reason": "do not print me"}},
        ],
    }


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.chats = []
        self.runs = []
        self.code = CODE
        self.user = PINS.user
        self.profile = {"id": PINS.reviewer, "resolved": {"model": "test/model", "project_ids": []}}
        self.schedules = []
        self.response = {"workflow_id": RUN, "run_id": EXECUTION, "workflow": audit.NAME, "trigger": "manual"}
        self.run_owner = PINS.user
        self.statuses = ["COMPLETED"]
        self.history = history()

    def call(self, path, method, body, *, timeout):
        self.calls.append((method, path, body))
        assert timeout <= 30
        if path == "/api/system?refresh=true":
            return {**BUILD, "auth_enabled": True}
        if path == "/api/auth/me":
            return {"user": {"id": self.user, "status": "active", "email_verified": True}}
        if path == audit.WORKFLOW:
            return {
                "code": self.code,
                "settings": {"disabled": False},
                "schedule_status": "available",
                "schedules": self.schedules,
            }
        if path == "/api/agents/" + PINS.reviewer:
            return self.profile
        if path == audit.CHATS:
            return {"chats": deepcopy(self.chats), "next_offset": None}
        if path == audit.SESSIONS:
            return {"sessions": [], "next_offset": None}
        if path == audit.RUNS:
            return {"runs": self.runs, "next_cursor": ""}
        if path == "/api/chats" and method == "POST":
            assert body["agent_id"] == PINS.reviewer
            assert body["title"].startswith("Synthetic staging issue audit QA ")
            row = {
                "id": "fixture",
                "title": body["title"],
                "owner_user_id": PINS.user,
                "session_count": 0,
                "message_count": 0,
                "project_ids": [],
            }
            self.chats.append(row)
            return row
        if path == audit.WORKFLOW + "/run" and method == "POST":
            assert body == {"input": {"days": 7, "dry_run": True, "review_agent_id": PINS.reviewer}}
            if isinstance(self.response, Exception):
                raise self.response
            return self.response
        if path == RUN_PATH:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if isinstance(status, Exception):
                raise status
            return {
                "run": {"owner_user_id": self.run_owner, "workflow_id": RUN, "run_id": EXECUTION},
                "temporal": {"status": status, "run_id": EXECUTION},
            }
        if path == RUN_PATH + f"/history?limit={audit.HISTORY_LIMIT}":
            return self.history
        if path == "/api/chats/fixture" and method == "DELETE":
            self.chats.clear()
            return {"ok": True}
        pytest.fail(f"unexpected {method} request")

    @property
    def mutations(self):
        return [(method, path) for method, path, _ in self.calls if method != "GET"]


@pytest.fixture
def stage(monkeypatch):
    monkeypatch.setattr(audit, "health", lambda: BUILD)
    now = [100.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(audit.time, "sleep", sleep)
    return FakeAPI()


def test_success_only_creates_fixture_starts_once_and_deletes_fixture(stage, capsys):
    stage.statuses = [APIError("unlogged", 503), "RUNNING", "COMPLETED"]
    audit.check(stage, PINS)
    assert stage.mutations == [
        ("POST", "/api/chats"),
        ("POST", audit.WORKFLOW + "/run"),
        ("DELETE", "/api/chats/fixture"),
    ]
    assert capsys.readouterr().out == f"Staging audit run ID: {RUN}\n"


@pytest.mark.parametrize(
    "condition", ["wrong_user", "old_chat", "old_run", "wrong_digest", "schedule", "profile"]
)
def test_missing_prerequisites_never_write(stage, condition):
    if condition == "wrong_user":
        stage.user = "not-qa"
    elif condition == "old_chat":
        stage.chats = [{"id": "not-ours"}]
    elif condition == "old_run":
        stage.runs = [{"workflow_id": "not-ours"}]
    elif condition == "wrong_digest":
        stage.code = "unreviewed"
    elif condition == "schedule":
        stage.schedules = [{"paused": False}]
    else:
        stage.profile["resolved"]["project_ids"] = ["copied-project"]
    with pytest.raises(audit.Blocked):
        audit.check(stage, PINS)
    assert stage.mutations == []


@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "CANCELED", "TERMINATED"])
def test_known_failure_cleans_only_fixture(stage, status):
    stage.statuses = [status]
    with pytest.raises(audit.Blocked, match="terminal status"):
        audit.check(stage, PINS)
    assert stage.mutations[-1] == ("DELETE", "/api/chats/fixture")


def test_lost_start_response_is_not_retried(stage, capsys):
    stage.response = TimeoutError("unlogged payload")
    with pytest.raises(TimeoutError):
        audit.check(stage, PINS)
    assert stage.mutations == [("POST", "/api/chats"), ("POST", audit.WORKFLOW + "/run")]
    assert "unlogged payload" not in capsys.readouterr().err


def test_polling_timeout_leaves_fixture_for_reconciliation(stage, monkeypatch):
    stage.statuses = ["RUNNING"]
    monkeypatch.setattr(audit, "WAIT_SECONDS", 21)
    with pytest.raises(audit.Blocked, match="within the check budget"):
        audit.check(stage, PINS)
    assert sum(path == RUN_PATH for _, path, _ in stage.calls) == 3
    assert stage.mutations[-1] == ("POST", audit.WORKFLOW + "/run")


def test_wrong_run_owner_is_not_accepted_or_cleaned_up(stage):
    stage.run_owner = "other"
    with pytest.raises(audit.Blocked, match="not owned"):
        audit.check(stage, PINS)
    assert stage.mutations[-1] == ("POST", audit.WORKFLOW + "/run")


@pytest.mark.parametrize(
    "invalid", ["no_op", "failed_save", "wrong_run_file", "dry_run", "expired", "empty_file"]
)
def test_terminal_without_expected_history_is_not_success(invalid):
    value = history()
    if invalid == "no_op":
        value["events"] = [value["events"][0], value["events"][-1]]
    elif invalid == "failed_save":
        value["events"][1]["event"] = "activity.failed"
    elif invalid == "wrong_run_file":
        value["events"][1]["result"]["name"] = "audit-round-other-1.json"
    elif invalid == "dry_run":
        value["events"][0]["input"]["dry_run"] = 1
    elif invalid == "expired":
        value["history_status"] = "expired"
    else:
        value["events"][1]["result"]["bytes"] = 0
    with pytest.raises(audit.Blocked):
        audit.check_artifacts(value, RUN, PINS.inputs)


@pytest.mark.parametrize(
    "origin", ["https://app.denkzeug.ai", audit.STAGING + "/", audit.STAGING + ".example"]
)
def test_reject_other_origins(origin):
    with pytest.raises(audit.Blocked, match="exact canonical staging origin"):
        audit.Pins.load({"APP_URL": origin})


def test_different_public_sha_stops_before_login(monkeypatch, capsys):
    monkeypatch.setattr(audit.Pins, "load", lambda _: PINS)
    monkeypatch.setattr(audit, "health", lambda: {**BUILD, "build_commit": "b" * 40})
    monkeypatch.setattr(audit, "API", lambda: pytest.fail("must not authenticate"))
    assert audit.main() == 1
    assert "exact deployed commit" in capsys.readouterr().err
