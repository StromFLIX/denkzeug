"""Readable workflow task titles without copying webhook payloads into chat previews."""

from __future__ import annotations

import sqlite3


def fallback_title(workflow: str | None, agent: str | None = None) -> str:
    if workflow:
        label = " ".join(workflow.replace("_", " ").replace("-", " ").split())
        if label:
            return (label[0].upper() + label[1:])[:120].rstrip()
    return f"Workflow run · {agent}"[:120] if agent else "Workflow run"


def title_input(workflow: str, task: str) -> str:
    # Incident details often follow standing instructions and an output schema.
    if len(task) > 11_800:
        task = task[:5900] + "\n[…]\n" + task[-5895:]
    return f"Workflow: {fallback_title(workflow)}\n\n{task}"


def repair_legacy_titles(conn: sqlite3.Connection) -> None:
    """Relabel only the original placeholders, without calling models on startup."""
    for row in conn.execute(
        "SELECT c.id, c.agent_name, m.id AS message_id, "
        "json_extract(m.meta, '$.origin.workflow') AS workflow "
        "FROM workflow_agent_tasks t JOIN chats c ON c.id = t.chat_id "
        "LEFT JOIN messages m ON m.rowid = (SELECT rowid FROM messages WHERE chat_id = c.id "
        "AND role = 'user' ORDER BY rowid LIMIT 1) "
        "WHERE c.title = 'Workflow agent task' AND c.title_state = 'manual' AND c.title_revision = 1"
    ):
        # A retry can still submit the first message if admission was interrupted.
        title = fallback_title(row["workflow"], row["agent_name"]) if row["message_id"] else "New chat"
        conn.execute(
            "UPDATE chats SET title = ?, title_state = 'provisional', title_revision = title_revision + 1 "
            "WHERE id = ? AND title_revision = 1",
            (title, row["id"]),
        )
