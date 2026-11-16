"""Transactional workflow-result outbox. Retrying delivery never reruns work."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .db import db


def finish(workflow_id: str, status: str, result: Any, *, notify: bool) -> None:
    """Record the terminal result and its delivery obligation in one commit."""
    # Expensive/untrusted schema validation must not hold SQLite's write lock.
    # The immutable revision selected here is pinned atomically below.
    from .local_apps import prepare_workflow, snapshot_workflow

    row = db.one("SELECT * FROM runs WHERE workflow_id=?", (workflow_id,))
    if row is None:
        raise ValueError("Cannot finish an unrecorded workflow run")
    if db.one("SELECT 1 FROM run_deliveries WHERE run_id=?", (row["id"],)):
        return
    prepared = prepare_workflow(row, result) if status == "completed" and notify else None
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM runs WHERE workflow_id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise ValueError("Cannot finish an unrecorded workflow run")
        if conn.execute("SELECT 1 FROM run_deliveries WHERE run_id = ?", (row["id"],)).fetchone():
            return  # A duplicate watcher must not overwrite an already sealed result.
        if status == "completed" and notify:
            snapshot_workflow(conn, row, prepared)
        conn.execute(
            "UPDATE runs SET status = ?, result = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(result) if result is not None else None, time.time(), row["id"]),
        )
        conn.execute(
            "INSERT INTO run_deliveries (run_id, state) VALUES (?,?)",
            (row["id"], "pending" if notify else "skipped"),
        )


def pending() -> list[dict]:
    return db.query(
        "SELECT r.workflow, r.workflow_id, r.status, r.result FROM run_deliveries d "
        "JOIN runs r ON r.id = d.run_id WHERE d.state = 'pending' AND d.retry_at <= ? "
        "ORDER BY d.retry_at, r.created_at LIMIT 100",
        (time.time(),),
    )


def failed(workflow_id: str) -> None:
    # Persist bounded backoff, not exception text (which may contain payloads).
    db.execute(
        "UPDATE run_deliveries SET attempts = attempts + 1, retry_at = ? + "
        "MIN(300, (1 << MIN(attempts + 1, 8))) WHERE state = 'pending' "
        "AND run_id IN (SELECT id FROM runs WHERE workflow_id = ?)",
        (time.time(), workflow_id),
    )


def commit(
    name: str,
    workflow_id: str,
    body: str,
    meta: dict,
    *,
    title: str,
    agent_set: str,
    model: str,
) -> str | None:
    """Create chat/message/inbox and mark delivered atomically, exactly once locally.

    Delivered tombstones deliberately survive chat deletion; a late retry must
    never resurrect a conversation the user removed.
    """
    now = time.time()
    message_id = hashlib.sha256(f"workflow-result:{workflow_id}".encode()).hexdigest()
    with db.transaction() as conn:
        run = conn.execute("SELECT id,owner_user_id FROM runs WHERE workflow_id=?", (workflow_id,)).fetchone()
        from .access import human_id

        owner = run["owner_user_id"] if run else human_id()
        delivery = conn.execute(
            "SELECT d.* FROM run_deliveries d JOIN runs r ON r.id = d.run_id WHERE r.workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if delivery is not None and delivery["state"] != "pending":
            return None
        previous = conn.execute("SELECT chat_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if previous is not None:
            chat_id = previous["chat_id"]
        else:
            config = conn.execute("SELECT * FROM workflow_settings WHERE name = ?", (name,)).fetchone()
            same = config is None or config["chat_mode"] == "same"
            private_target = (
                conn.execute(
                    "SELECT a.chat_id FROM access_workflow_chats a JOIN chats c ON c.id=a.chat_id "
                    "WHERE a.workflow=? AND a.user_id=? AND c.owner_user_id=?",
                    (name, owner, owner),
                ).fetchone()
                if same and owner
                else None
            )
            chat_id = (
                private_target["chat_id"]
                if private_target
                else config["chat_id"]
                if config and same
                else None
            )
            if (
                chat_id
                and not conn.execute(
                    "SELECT 1 FROM chats WHERE id = ? AND owner_user_id IS ?", (chat_id, owner)
                ).fetchone()
            ):
                chat_id = None
            if not chat_id:
                chat_id = uuid.uuid4().hex[:12]
                suffix = "" if same else time.strftime(" %d %b %H:%M")
                conn.execute(
                    "INSERT INTO chats (id, title, agent_set, model, created_at, updated_at, "
                    "last_message_at, last_user_message_at, last_activity_at, title_state, owner_user_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'manual',?)",
                    (chat_id, (title + suffix)[:120], agent_set, model, now, now, now, now, now, owner),
                )
                if same:
                    resource = conn.execute(
                        "SELECT owner_user_id FROM access_resources WHERE kind='workflow' AND resource_id=?",
                        (name,),
                    ).fetchone()
                    workspace_owner = resource["owner_user_id"] if resource else owner
                    if workspace_owner == owner and (config is None or config["owner_user_id"] == owner):
                        conn.execute(
                            "INSERT INTO workflow_settings (name, chat_id, owner_user_id) VALUES (?,?,?) "
                            "ON CONFLICT(name) DO UPDATE SET chat_id = excluded.chat_id",
                            (name, chat_id, owner),
                        )
            if same and owner:
                conn.execute(
                    "INSERT INTO access_workflow_chats VALUES(?,?,?) ON CONFLICT(workflow,user_id) "
                    "DO UPDATE SET chat_id=excluded.chat_id",
                    (name, owner, chat_id),
                )
            if run:
                from .local_apps import attach_workflow

                body, meta = attach_workflow(conn, run["id"], chat_id, body, meta)
            conn.execute(
                "INSERT INTO messages (id, chat_id, role, content, meta, created_at) "
                "VALUES (?,?,'assistant',?,?,?)",
                (message_id, chat_id, body, json.dumps(meta), now),
            )
            conn.execute(
                "INSERT INTO chat_context_messages (message_id, source_id) VALUES (?,?)",
                (message_id, message_id),
            )
            conn.execute(
                "UPDATE chats SET updated_at = ?, last_message_at = ?, last_activity_at = ? WHERE id = ?",
                (now, now, now, chat_id),
            )
        if delivery is not None:
            conn.execute(
                "UPDATE run_deliveries SET state = 'delivered', chat_id = ?, message_id = ? WHERE run_id = ?",
                (chat_id, message_id, delivery["run_id"]),
            )
    return chat_id
