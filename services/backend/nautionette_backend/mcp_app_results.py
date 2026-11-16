"""Persist MCP App snapshots at the agent/browser boundary, never in model text."""

from __future__ import annotations

import hashlib
import json
import time

from fastapi import HTTPException

from . import access, local_apps
from .app_sandbox import origin


def install_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mcp_app_instances (id TEXT PRIMARY KEY, "
        "chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, "
        "turn_id TEXT NOT NULL REFERENCES chat_turns(id) ON DELETE CASCADE, "
        "owner_user_id TEXT NOT NULL, payload TEXT NOT NULL, size_bytes INTEGER NOT NULL, "
        "created_at REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS mcp_app_instances_owner ON mcp_app_instances(owner_user_id)")


def approved_csp(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Invalid resource policy")
    result = {}
    for key in ("resourceDomains", "connectDomains"):
        domains = value.get(key, [])
        if not isinstance(domains, list) or len(domains) > 24:
            raise ValueError("Invalid resource domains")
        result[key] = sorted({origin(item, local=False) for item in domains})
    return result


def capture_result(job: dict, event: dict) -> dict:
    """Business success survives snapshot failures. Never retain raw HTML in events."""
    from .db import db

    event = dict(event)
    candidate = event.pop("mcp_app", None)
    try:
        if not job.get("chat_id") or not job.get("turn_id") or event.get("error"):
            return event
        if not isinstance(candidate, dict) or not isinstance(candidate.get("html"), str):
            raise ValueError("Invalid app snapshot")
        if event.get("type") != "tool_done" or not isinstance(event.get("id"), str):
            raise ValueError("Invalid app event")
        # Authority comes from the live turn, never from producer-supplied data.
        turn = db.one(
            "SELECT c.owner_user_id FROM chat_turns t JOIN chats c ON c.id=t.chat_id "
            "WHERE t.id=? AND c.id=? AND t.state='running' AND t.stop_requested=0",
            (job["turn_id"], job["chat_id"]),
        )
        if not turn or not turn["owner_user_id"]:
            raise ValueError("App turn is no longer live")
        uri = candidate.get("resource_uri")
        if not isinstance(uri, str) or not uri.startswith("ui://") or len(uri) > 2048:
            raise ValueError("Invalid UI resource")
        html = candidate["html"]
        if not html or len(html.encode()) > local_apps.MAX_HTML_BYTES:
            raise ValueError("App HTML exceeds the limit")
        result = candidate.get("result")
        arguments = candidate.get("input", {})
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("content", []), list)
            or not isinstance(arguments, dict)
        ):
            raise ValueError("Invalid app input/result")
        title = str(candidate.get("title") or event.get("name") or "Interactive result")[:160]
        payload = {
            "html": html,
            "html_sha256": hashlib.sha256(html.encode()).hexdigest(),
            "title": title,
            "input": arguments,
            "result": result,
            "csp": approved_csp(candidate.get("csp", {})),
            "source": {"kind": "mcp", "tool": event.get("name"), "resource_uri": uri},
        }
        encoded = local_apps.encoded(payload, 2 * 1024 * 1024, "MCP App snapshot")
        size = len(encoded.encode())
        instance_id = hashlib.sha256(f"mcp-app:{job['turn_id']}:{event['id']}".encode()).hexdigest()
        with db.transaction() as conn:
            existing = conn.execute("SELECT id FROM mcp_app_instances WHERE id=?", (instance_id,)).fetchone()
            if not existing:
                used, count = conn.execute(
                    "SELECT COALESCE(SUM(size_bytes),0),COUNT(*) FROM mcp_app_instances "
                    "WHERE owner_user_id=?",
                    (turn["owner_user_id"],),
                ).fetchone()
                if used + size > local_apps.MAX_INSTANCE_BYTES or count >= local_apps.MAX_INSTANCES:
                    raise ValueError("MCP App snapshot storage limit reached")
                conn.execute(
                    "INSERT INTO mcp_app_instances VALUES(?,?,?,?,?,?,?)",
                    (
                        instance_id,
                        job["chat_id"],
                        job["turn_id"],
                        turn["owner_user_id"],
                        encoded,
                        size,
                        time.time(),
                    ),
                )
        event["app"] = {"id": instance_id, "title": title}
    except (HTTPException, ValueError, TypeError, UnicodeError, RecursionError):
        event["app_error"] = "The interactive view could not be saved. The original tool result is unchanged."
    return event


def get_instance(chat_id: str, instance_id: str) -> dict:
    from .db import db

    access.require_chat(chat_id)
    row = db.one(
        "SELECT payload FROM mcp_app_instances WHERE id=? AND chat_id=? AND owner_user_id=?",
        (instance_id, chat_id, local_apps.owner()),
    )
    if not row:
        raise HTTPException(404, "App instance not found")
    return json.loads(row["payload"])
