"""Owner-private, immutable single-file MCP Apps and durable workflow snapshots.

HTML is authored code, never executed by the server. Consumers must serve it in
an isolated MCP App sandbox with the returned restrictive resource CSP.
Published code is trusted by its owner to receive that owner's workflow data;
a browser iframe is not an OS-level network isolation boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import access

MAX_APPS = 100
MAX_REVISIONS = 100
MAX_HTML_BYTES = 1024 * 1024
MAX_DATA_BYTES = 1024 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
MAX_REGISTRY_BYTES = 32 * 1024 * 1024
MAX_INSTANCE_BYTES = 64 * 1024 * 1024
MAX_INSTANCES = 1000
SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
CSP = {"connectDomains": [], "resourceDomains": []}


def install_schema(conn: sqlite3.Connection) -> None:
    for statement in (
        "CREATE TABLE IF NOT EXISTS local_apps (owner_user_id TEXT NOT NULL, id TEXT NOT NULL, "
        "title TEXT NOT NULL, description TEXT NOT NULL, active_revision INTEGER NOT NULL, "
        "archived INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, "
        "PRIMARY KEY(owner_user_id,id))",
        "CREATE TABLE IF NOT EXISTS local_app_revisions (owner_user_id TEXT NOT NULL, app_id TEXT NOT NULL, "
        "revision INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, html TEXT NOT NULL, "
        "html_sha256 TEXT NOT NULL, input_schema TEXT NOT NULL, sample_data TEXT NOT NULL, "
        "change_note TEXT NOT NULL, created_at REAL NOT NULL, size_bytes INTEGER NOT NULL, "
        "PRIMARY KEY(owner_user_id,app_id,revision), FOREIGN KEY(owner_user_id,app_id) "
        "REFERENCES local_apps(owner_user_id,id))",
        "CREATE TABLE IF NOT EXISTS app_instances (id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, "
        "chat_id TEXT REFERENCES chats(id) ON DELETE CASCADE, "
        "run_id TEXT UNIQUE REFERENCES runs(id) ON DELETE SET NULL, app_id TEXT NOT NULL, "
        "revision INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, data TEXT NOT NULL, "
        "created_at REAL NOT NULL, size_bytes INTEGER NOT NULL, "
        "FOREIGN KEY(owner_user_id,app_id,revision) "
        "REFERENCES local_app_revisions(owner_user_id,app_id,revision))",
        "CREATE INDEX IF NOT EXISTS app_instances_owner ON app_instances(owner_user_id)",
        "CREATE TABLE IF NOT EXISTS workflow_app_diagnostics (run_id TEXT PRIMARY KEY "
        "REFERENCES runs(id) ON DELETE CASCADE, code TEXT NOT NULL)",
    ):
        conn.execute(statement)


def owner() -> str:
    value = access.human_id()
    if not value:
        raise HTTPException(403, "An acting user account is required")
    return value


def encoded(value: Any, limit: int, label: str) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(text.encode())
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise HTTPException(422, f"{label} must be finite JSON") from exc
    if size > limit:
        raise HTTPException(413, f"{label} exceeds {limit} bytes")
    return text


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.has_html = False

    def handle_starttag(self, tag, attrs):
        self.has_html |= tag == "html"
        for name, value in attrs:
            if name in {"src", "srcset", "poster", "data"} or (tag == "link" and name == "href"):
                if value and not value.startswith(("data:", "blob:", "#")):
                    raise HTTPException(422, "App HTML must be self-contained (inline its resources)")
        if tag == "base":
            raise HTTPException(422, "App HTML cannot change its base URL")


def validate_html(html: str) -> None:
    try:
        if len(html.encode()) > MAX_HTML_BYTES:
            raise HTTPException(413, "App HTML exceeds 1 MiB")
        parser = _HTML()
        parser.feed(html)
        parser.close()
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, "Invalid HTML document") from exc
    if not parser.has_html or "\x00" in html:
        raise HTTPException(422, "Provide a complete HTML document with an html element")


def validate_schema_shape(schema: dict) -> None:
    encoded(schema, MAX_SCHEMA_BYTES, "Input schema")
    # Ban all external identifiers/references, including unused branches. A
    # retrieval-denying registry is a second boundary against network/file I/O.
    remaining = [(schema, 0)]
    nodes = 0
    while remaining:
        value, depth = remaining.pop()
        nodes += 1
        if depth > 32 or nodes > 5000:
            raise HTTPException(422, "Input schema is too complex")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$id"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise HTTPException(422, "Input schema references must be local fragments")
                remaining.append((child, depth + 1))
        elif isinstance(value, list):
            remaining.extend((child, depth + 1) for child in value)


def validate_data(schema: dict, data: dict) -> str:
    text = encoded(data, MAX_DATA_BYTES, "App data")
    validate_schema_shape(schema)
    try:
        checked = subprocess.run(  # noqa: S603 - fixed own interpreter/worker; data only on stdin
            [sys.executable, "-I", str(Path(__file__).with_name("app_schema.py"))],
            input=json.dumps({"schema": schema, "data": data}).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(422, "App validation exceeded its safe execution limit") from exc
    if checked.returncode != 0 or checked.stdout != b"ok":
        raise HTTPException(422, "App schema/data is invalid or exceeded safe validation limits")
    return text


def _summary(row) -> dict:
    value = dict(row)
    value.pop("owner_user_id", None)
    value["archived"] = bool(value["archived"])
    return value


def _revision(row) -> dict:
    value = dict(row)
    value.pop("owner_user_id", None)
    value.pop("size_bytes", None)
    value["input_schema"] = json.loads(value["input_schema"])
    value["sample_data"] = json.loads(value["sample_data"])
    value["csp"] = {key: list(domains) for key, domains in CSP.items()}
    return value


def list_apps() -> dict:
    from .db import db

    return {
        "apps": [
            _summary(row)
            for row in db.query(
                "SELECT * FROM local_apps WHERE owner_user_id=? ORDER BY updated_at DESC,id", (owner(),)
            )
        ]
    }


def get_app(app_id: str) -> dict:
    from .db import db

    user = owner()
    row = db.one("SELECT * FROM local_apps WHERE owner_user_id=? AND id=?", (user, app_id))
    if not row:
        raise HTTPException(404, "App not found")
    revisions = db.query(
        "SELECT revision,title,description,change_note,html_sha256,created_at FROM local_app_revisions "
        "WHERE owner_user_id=? AND app_id=? ORDER BY revision DESC",
        (user, app_id),
    )
    return {
        "app": _summary(row),
        "revisions": revisions,
        "revision": get_revision(app_id, row["active_revision"]),
        "usage": {
            "saved_results": db.one(
                "SELECT COUNT(*) AS n FROM app_instances WHERE owner_user_id=? AND app_id=?",
                (user, app_id),
            )["n"],
            "workflows": [
                item["workflow"]
                for item in db.query(
                    "SELECT DISTINCT r.workflow FROM app_instances i JOIN runs r ON r.id=i.run_id "
                    "WHERE i.owner_user_id=? AND i.app_id=? ORDER BY r.workflow",
                    (user, app_id),
                )
            ],
        },
    }


def get_revision(app_id: str, revision: int) -> dict:
    from .db import db

    row = db.one(
        "SELECT * FROM local_app_revisions WHERE owner_user_id=? AND app_id=? AND revision=?",
        (owner(), app_id, revision),
    )
    if not row:
        raise HTTPException(404, "App revision not found")
    return _revision(row)


def publish(payload) -> dict:
    from .db import db

    user = owner()
    validate_html(payload.html)
    data = validate_data(payload.input_schema, payload.sample_data)
    schema = encoded(payload.input_schema, MAX_SCHEMA_BYTES, "Input schema")
    size = len(payload.html.encode()) + len(data.encode()) + len(schema.encode())
    now = time.time()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM local_apps WHERE owner_user_id=? AND id=?", (user, payload.id)
        ).fetchone()
        current = row["active_revision"] if row else 0
        if payload.expected_revision != current:
            raise HTTPException(409, "App revision changed; reload before publishing")
        if row and row["archived"]:
            raise HTTPException(409, "Unarchive this app before publishing")
        if current >= MAX_REVISIONS:
            raise HTTPException(409, "App revision limit reached")
        if (
            not row
            and conn.execute("SELECT COUNT(*) FROM local_apps WHERE owner_user_id=?", (user,)).fetchone()[0]
            >= MAX_APPS
        ):
            raise HTTPException(409, "App count limit reached")
        used = conn.execute(
            "SELECT COALESCE(SUM(size_bytes),0) FROM local_app_revisions WHERE owner_user_id=?", (user,)
        ).fetchone()[0]
        if used + size > MAX_REGISTRY_BYTES:
            raise HTTPException(413, "App registry storage limit reached")
        revision = current + 1
        conn.execute(
            "INSERT INTO local_apps VALUES(?,?,?,?,?,0,?) ON CONFLICT(owner_user_id,id) DO UPDATE SET "
            "title=excluded.title,description=excluded.description,active_revision=excluded.active_revision,"
            "updated_at=excluded.updated_at",
            (user, payload.id, payload.title, payload.description, revision, now),
        )
        conn.execute(
            "INSERT INTO local_app_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user,
                payload.id,
                revision,
                payload.title,
                payload.description,
                payload.html,
                hashlib.sha256(payload.html.encode()).hexdigest(),
                schema,
                data,
                payload.change_note,
                now,
                size,
            ),
        )
    return get_app(payload.id)


def archive(app_id: str, archived: bool, expected_revision: int) -> dict:
    from .db import db

    user = owner()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM local_apps WHERE owner_user_id=? AND id=?", (user, app_id)
        ).fetchone()
        if not row:
            raise HTTPException(404, "App not found")
        if row["active_revision"] != expected_revision:
            raise HTTPException(409, "App revision changed; reload before archiving")
        conn.execute(
            "UPDATE local_apps SET archived=?,updated_at=? WHERE owner_user_id=? AND id=?",
            (int(archived), time.time(), user, app_id),
        )
    return get_app(app_id)


def prepare_workflow(run, result) -> dict | None:
    """Resolve/validate outside the event loop and outside the completion lock."""
    from .db import db

    if not isinstance(result, dict) or "ui" not in result:
        return
    try:
        ui, summary, data = result["ui"], result.get("summary"), result.get("data")
        if (
            not isinstance(ui, dict)
            or set(ui) - {"app", "revision"}
            or not isinstance(ui.get("app"), str)
            or not SLUG.fullmatch(ui["app"])
            or not isinstance(summary, str)
            or not isinstance(data, dict)
        ):
            raise HTTPException(422, "Invalid app result envelope")
        encoded(summary, 64 * 1024, "App summary")
        app = db.one(
            "SELECT * FROM local_apps WHERE owner_user_id=? AND id=?", (run["owner_user_id"], ui["app"])
        )
        if not app or app["archived"]:
            raise HTTPException(422, "App unavailable")
        revision = ui.get("revision", app["active_revision"])
        if type(revision) is not int or revision < 1:
            raise HTTPException(422, "Invalid app revision")
        source = db.one(
            "SELECT * FROM local_app_revisions WHERE owner_user_id=? AND app_id=? AND revision=?",
            (run["owner_user_id"], app["id"], revision),
        )
        if not source:
            raise HTTPException(422, "App revision unavailable")
        data_json = validate_data(json.loads(source["input_schema"]), data)
        return {
            "app_id": app["id"],
            "revision": revision,
            "title": source["title"],
            "data_json": data_json,
            "summary": summary,
            "size": len(data_json.encode()) + len(summary.encode()),
        }
    except HTTPException:
        return {"invalid": True}


def snapshot_workflow(conn, run, prepared) -> None:
    """Atomic pin/outbox, with no executable schema work inside the transaction."""
    if prepared is None:
        return
    try:
        if prepared.get("invalid"):
            raise HTTPException(422, "Invalid app result")
        app = conn.execute(
            "SELECT * FROM local_apps WHERE owner_user_id=? AND id=?",
            (run["owner_user_id"], prepared["app_id"]),
        ).fetchone()
        if not app or app["archived"]:
            raise HTTPException(422, "App unavailable")
        size = prepared["size"]
        used, count = conn.execute(
            "SELECT COALESCE(SUM(size_bytes),0),COUNT(*) FROM app_instances WHERE owner_user_id=?",
            (run["owner_user_id"],),
        ).fetchone()
        if used + size > MAX_INSTANCE_BYTES or count >= MAX_INSTANCES:
            raise HTTPException(413, "App instance storage limit reached")
        conn.execute(
            "INSERT INTO app_instances VALUES(?,?,NULL,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                run["owner_user_id"],
                run["id"],
                app["id"],
                prepared["revision"],
                prepared["title"],
                prepared["summary"],
                prepared["data_json"],
                time.time(),
                size,
            ),
        )
    except HTTPException:
        # Do not reflect data or schema errors into public diagnostics.
        conn.execute("INSERT INTO workflow_app_diagnostics VALUES(?,?)", (run["id"], "invalid_app_result"))


def attach_workflow(conn, run_id: str, chat_id: str, body: str, meta: dict) -> tuple[str, dict]:
    instance = conn.execute("SELECT * FROM app_instances WHERE run_id=?", (run_id,)).fetchone()
    if instance:
        conn.execute("UPDATE app_instances SET chat_id=? WHERE id=?", (chat_id, instance["id"]))
        return instance["summary"], {
            **meta,
            "apps": [
                {
                    "id": instance["id"],
                    "title": instance["title"],
                    "app_id": instance["app_id"],
                    "revision": instance["revision"],
                }
            ],
        }
    diagnostic = conn.execute(
        "SELECT code FROM workflow_app_diagnostics WHERE run_id=?", (run_id,)
    ).fetchone()
    if diagnostic:
        return body, {**meta, "app_diagnostic": diagnostic["code"]}
    return body, meta


def get_instance(chat_id: str, instance_id: str) -> dict:
    from .db import db

    access.require_chat(chat_id)
    row = db.one(
        "SELECT i.*,r.html,r.html_sha256 FROM app_instances i JOIN local_app_revisions r "
        "ON r.owner_user_id=i.owner_user_id AND r.app_id=i.app_id AND r.revision=i.revision "
        "WHERE i.id=? AND i.chat_id=? AND i.owner_user_id=?",
        (instance_id, chat_id, owner()),
    )
    if not row:
        from .mcp_app_results import get_instance as get_mcp_instance

        return get_mcp_instance(chat_id, instance_id)
    return {
        "html": row["html"],
        "input": {},
        "result": {
            "content": [{"type": "text", "text": row["summary"]}],
            "structuredContent": json.loads(row["data"]),
        },
        "title": row["title"],
        "app_id": row["app_id"],
        "revision": row["revision"],
        "source": "workflow",
        "html_sha256": row["html_sha256"],
        "csp": CSP,
    }
