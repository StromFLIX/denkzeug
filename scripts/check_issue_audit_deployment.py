"""Explicit staging-only smoke of the *existing* weekly issue audit.

The trusted staging environment pins the QA identity, reviewer and reviewed
workflow digest. A dispatch asserts exclusive QA use and review of non-chat
inputs/model destination. HTTP preflight cannot lock a deployment, workflow or
account; keep them unchanged until terminal. A nonempty QA inventory is a real
blocker, not permission to delete or send existing evidence to the reviewer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from scripts.account_session import NoRedirect
from scripts.smoke_api import API, APIError

STAGING = "https://app.stage.denkzeug.ai"
NAME = "weekly_nautionette_issue_audit"
WORKFLOW = "/api/workflows/" + NAME
CHATS = "/api/chat-inspection/chats?limit=2"
SESSIONS = "/api/chat-inspection/sessions?limit=1"
RUNS = "/api/runs?compact=true&limit=1"
RUN_ID = re.compile(re.escape(NAME) + r"-[0-9]{10}-[a-f0-9]{6}")
CHAT_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")
EXECUTION_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
WAIT_SECONDS = 65 * 60  # The reviewed workflow's execution timeout is 60m.
HISTORY_LIMIT = 2000
FAILURES = {"failed", "timed_out", "terminated", "canceled"}


class Blocked(RuntimeError):
    """Only fixed, log-safe diagnostics may be passed to this exception."""


@dataclass(frozen=True)
class Pins:
    build: str
    digest: str
    user: str
    reviewer: str

    @classmethod
    def load(cls, env):
        if env.get("APP_URL") != STAGING:
            raise Blocked("APP_URL must be the exact canonical staging origin")
        if env.get("ISSUE_AUDIT_ISOLATED_QA") != "true":
            raise Blocked("Synthetic-only staging review and QA isolation must be confirmed")
        if (
            not env.get("DENKZEUG_SMOKE_USERNAME")
            or not env.get("DENKZEUG_SMOKE_PASSWORD")
            or env.get("DENKZEUG_DEVICE_TOKEN")
            or env.get("DENKZEUG_SMOKE_PASSWORD_FILE")
        ):
            raise Blocked("Configure the existing staging smoke username and password secrets only")
        values = []
        for key, pattern in (
            ("ISSUE_AUDIT_STAGING_SHA", r"[0-9a-f]{40}"),
            ("ISSUE_AUDIT_SOURCE_SHA256", r"[0-9a-f]{64}"),
            ("ISSUE_AUDIT_QA_USER_ID", r"[a-zA-Z0-9_-]{1,128}"),
            ("ISSUE_AUDIT_REVIEW_AGENT_ID", r"[a-zA-Z0-9_-]{1,128}"),
        ):
            value = env.get(key, "")
            if not re.fullmatch(pattern, value):
                raise Blocked("Missing or invalid staging pin: " + key)
            values.append(value)
        return cls(*values)

    @property
    def inputs(self):
        return {"days": 7, "dry_run": True, "review_agent_id": self.reviewer}


def require_stage(payload, pins):
    if payload.get("environment") != "staging" or payload.get("build_commit") != pins.build:
        raise Blocked("Staging environment or exact deployed commit did not match")


def health():
    # Check public build metadata before login. Never follow redirects even on health.
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(STAGING + "/healthz", timeout=15) as response:  # noqa: S310 - constant HTTPS
        return json.loads(response.read(16_384))


def request(api, path, method="GET", body=None, *, timeout=30):
    return api.call(path, method, body, timeout=timeout)


def check_source(api, pins):
    detail = request(api, WORKFLOW)
    code = detail.get("code")
    if not isinstance(code, str) or hashlib.sha256(code.encode("utf-8")).hexdigest() != pins.digest:
        raise Blocked("Existing staging workflow source does not match the reviewed digest")
    if detail.get("settings", {}).get("disabled") is not False:
        raise Blocked("Existing staging workflow is disabled or its status is unknown")
    # Member accounts cannot inspect schedules; the dispatch attestation must
    # cover paused schedules in that case. Never unpause or change any schedule.
    schedules = detail.get("schedules")
    if (
        detail.get("schedule_status") not in {"available", "hidden"}
        or not isinstance(schedules, list)
        or any(schedule.get("paused") is not True for schedule in schedules)
    ):
        raise Blocked("Existing audit schedule is active or could not be checked")


def check_inventory(api, chat_id=None, title=None):
    # Use the same ownership-scoped inventory as the reviewer, without a time
    # filter: even an old chat can be touched during the audit and become visible.
    chats = request(api, CHATS)
    sessions = request(api, SESSIONS)
    runs = request(api, RUNS)
    if (
        chats.get("next_offset", -1) is not None
        or sessions.get("next_offset", -1) is not None
        or sessions.get("sessions") != []
        or runs.get("next_cursor") != ""
        or runs.get("runs") != []
    ):
        raise Blocked("QA evidence is not isolated: existing sessions or runs, or incomplete inventory")
    rows = chats.get("chats")
    if chat_id is None:
        if rows != []:
            raise Blocked("QA account already owns chats; refusing to audit existing evidence")
    elif (
        not isinstance(rows, list)
        or len(rows) != 1
        or not (
            rows[0].get("id") == chat_id
            and rows[0].get("title") == title
            and rows[0].get("message_count") == 0
            and rows[0].get("session_count") == 0
            and rows[0].get("project_ids") == []
        )
    ):
        raise Blocked("QA inventory is not exactly the new synthetic metadata fixture")


def check_artifacts(history, run_id, inputs):
    if (
        history.get("workflow_id") != run_id
        or history.get("workflow") != NAME
        or history.get("trigger") != "manual"
        or history.get("status") != "completed"
        or history.get("history_status") != "available"
    ):
        raise Blocked("Terminal audit history is not the expected completed manual run")
    events = history.get("events")
    if not isinstance(events, list) or not 1 < len(events) < HISTORY_LIMIT:
        raise Blocked("Audit history is absent or may be truncated")
    start, end = events[0], events[-1]
    actual = start.get("input")
    if (
        start.get("event") != "workflow.started"
        or start.get("workflow_type") != NAME
        or not isinstance(actual, dict)
        or actual != inputs
        or type(actual.get("days")) is not int
        or actual.get("dry_run") is not True
        or end.get("event") != "workflow.completed"
    ):
        raise Blocked("Temporal history does not prove explicit dry-run inputs and completion")
    expected = {f"audit-round-{run_id}-1.json", f"audit-review-{run_id}.json", f"audit-final-{run_id}.json"}
    completed = set()
    for event in events:
        if event.get("event") != "activity.completed" or event.get("activity") != "save_artifact":
            continue
        result = event.get("result")
        if not isinstance(result, dict):
            continue
        name, path, size = result.get("name"), result.get("path"), result.get("bytes")
        if (
            isinstance(name, str)
            and name in expected
            and isinstance(path, str)
            and PurePosixPath(path).name == name
            and type(size) is int
            and size > 0
        ):
            completed.add(name)
    if completed != expected:
        raise Blocked("Audit completed without required nonempty save_artifact activity metadata")


def wait_for_terminal(api, path, run_id, execution_id, pins, deadline):
    while (left := deadline - time.monotonic()) > 0:
        try:
            detail = request(api, path, timeout=min(30, left))
        except APIError as exc:
            if exc.status not in {502, 503, 504}:
                raise
            # A transient status GET can be polled again, never a start request.
        else:
            row, remote = detail.get("run") or {}, detail.get("temporal") or {}
            if row.get("owner_user_id") != pins.user or row.get("workflow_id") != run_id:
                raise Blocked("Audit run is not owned by the pinned QA account; reconcile before retrying")
            if row.get("run_id") != execution_id or remote.get("run_id") != execution_id:
                raise Blocked("Temporal execution changed or is unavailable; reconcile before retrying")
            status = remote.get("status", "").lower()
            if status == "completed" or status in FAILURES:
                return status
            if status != "running":
                raise Blocked("Temporal audit state is unavailable; reconcile before retrying")
        time.sleep(max(0, min(10, deadline - time.monotonic())))
    raise Blocked("Audit did not reach terminal state within the check budget; reconcile before retrying")


def check(api, pins):
    system = request(api, "/api/system?refresh=true")
    require_stage(system, pins)
    if system.get("auth_enabled") is not True:
        raise Blocked("Staging account authentication is not enabled")
    who = request(api, "/api/auth/me").get("user", {})
    if who.get("id") != pins.user or who.get("status") != "active" or who.get("email_verified") is not True:
        raise Blocked("Signed-in account is not the pinned active, verified staging QA user")
    check_source(api, pins)
    profile = request(api, "/api/agents/" + pins.reviewer)
    resolved = profile.get("resolved", {})
    if profile.get("id") != pins.reviewer or not resolved.get("model") or resolved.get("project_ids") != []:
        raise Blocked("Approved staging review profile is missing, lacks a model, or has project context")
    check_inventory(api)

    # Create only a recognizable title/metadata fixture. Do not submit a chat
    # turn: that would itself invoke the selected agent/model and possibly tools.
    title = "Synthetic staging issue audit QA " + uuid.uuid4().hex
    chat = request(api, "/api/chats", "POST", {"title": title, "agent_id": pins.reviewer})
    chat_id = chat.get("id", "")
    if (
        not isinstance(chat_id, str)
        or not CHAT_ID.fullmatch(chat_id)
        or chat.get("owner_user_id") != pins.user
        or chat.get("title") != title
    ):
        raise Blocked("Synthetic chat creation could not be verified; reconcile the QA inventory")
    may_be_active = False
    try:
        check_inventory(api, chat_id, title)
        require_stage(health(), pins)
        check_source(api, pins)
        may_be_active = True  # Lost response is not evidence that no run started.
        start = request(api, WORKFLOW + "/run", "POST", {"input": pins.inputs})
        run_id = start.get("workflow_id", "")
        execution_id = start.get("run_id", "")
        if (
            not isinstance(run_id, str)
            or not RUN_ID.fullmatch(run_id)
            or not isinstance(execution_id, str)
            or not EXECUTION_ID.fullmatch(execution_id)
            or start.get("workflow") != NAME
            or start.get("trigger") != "manual"
        ):
            raise Blocked("Audit start could not be verified; reconcile before retrying")
        print("Staging audit run ID: " + run_id, flush=True)
        path = "/api/runs/" + run_id
        deadline = time.monotonic() + WAIT_SECONDS
        outcome = wait_for_terminal(api, path, run_id, execution_id, pins, deadline)
        may_be_active = False
        if outcome != "completed":
            raise Blocked("Staging audit reached terminal status: " + outcome)
        check_artifacts(request(api, path + f"/history?limit={HISTORY_LIMIT}"), run_id, pins.inputs)
        require_stage(health(), pins)
        check_source(api, pins)
    finally:
        if not may_be_active:
            # Only this script's verified fixture; no retry or deletion of any other chat.
            original_error = sys.exception()
            try:
                request(api, "/api/chats/" + chat_id, "DELETE")
            except Exception as exc:
                message = "Synthetic fixture cleanup could not be confirmed; reconcile manually."
                if original_error is None:
                    raise Blocked(message) from exc
                print(message, file=sys.stderr)
        else:
            print(
                "Start or terminal state unconfirmed; left synthetic fixture for reconciliation.",
                file=sys.stderr,
            )


def main():
    try:
        pins = Pins.load(os.environ)
        require_stage(health(), pins)
        with API() as api:
            check(api, pins)
    except Blocked as exc:
        print(f"Blocked: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never print response bodies, secrets, or tracebacks
        print(
            "Blocked: staging request failed or returned an unexpected response; reconcile before retrying.",
            file=sys.stderr,
        )
        return 1
    print("Passed: completed staging dry run with round, review and final artifact metadata.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
