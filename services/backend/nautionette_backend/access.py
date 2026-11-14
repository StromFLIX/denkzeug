"""Application-level private ownership and explicit workspace grants.

Private objects are never made public by an administrator role. Service tokens
are transport credentials, not administrator accounts. New routes are denied to
members until reviewed here; a grant never permits arbitrary code execution.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

KINDS = frozenset({"agent", "project", "workflow", "app", "tool", "repository"})
OWNED_TABLES = {"chats": "id", "runs": "id", "mini_apps": "id", "workflow_settings": "name"}


def principal():
    # Lazy import: Database is initialized before security/auth storage.
    from . import security

    context = getattr(security, "current_principal", None)
    return context.get() if context is not None else None


def human_id() -> str | None:
    who = principal()
    return who.user_id if who is not None and not who.service else None


def install_access_schema(conn: sqlite3.Connection) -> None:
    """Idempotent migration, using the caller's transaction (no executescript)."""
    conn.create_function("access_user_id", 0, human_id)
    for statement in (
        "CREATE TABLE IF NOT EXISTS access_state (id INTEGER PRIMARY KEY CHECK(id=1), legacy_owner TEXT)",
        "INSERT OR IGNORE INTO access_state(id) VALUES(1)",
        "CREATE TABLE IF NOT EXISTS access_resources (kind TEXT NOT NULL, resource_id TEXT NOT NULL, "
        "owner_user_id TEXT NOT NULL, PRIMARY KEY(kind,resource_id))",
        "CREATE TABLE IF NOT EXISTS access_grants (kind TEXT NOT NULL, resource_id TEXT NOT NULL, "
        "user_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(kind,resource_id,user_id))",
    ):
        conn.execute(statement)
    for table, key in OWNED_TABLES.items():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
        if "owner_user_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_user_id TEXT")  # noqa: S608
        conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_owner ON {table}(owner_user_id)")  # noqa: S608
        inherited = "NULL"
        if table == "runs":
            inherited = (
                "(SELECT owner_user_id FROM access_resources "
                "WHERE kind='workflow' AND resource_id=NEW.workflow)"
            )
        elif table == "chats":
            inherited = "(SELECT owner_user_id FROM chats WHERE id=NEW.delegated_parent_chat_id)"
        elif table == "workflow_settings":
            inherited = (
                "(SELECT owner_user_id FROM access_resources WHERE kind='workflow' AND resource_id=NEW.name)"
            )
        # Prior to bootstrap unowned legacy writes remain migratable. Once the
        # first account exists, missing context cannot create orphan private data.
        owner = f"COALESCE(NEW.owner_user_id,access_user_id(),{inherited})"
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_access_insert AFTER INSERT ON {table} BEGIN "  # noqa: S608
            f"UPDATE {table} SET owner_user_id={owner} WHERE {key}=NEW.{key}; "
            f"SELECT CASE WHEN (SELECT legacy_owner FROM access_state WHERE id=1) IS NOT NULL "
            f"AND (SELECT owner_user_id FROM {table} WHERE {key}=NEW.{key}) IS NULL "
            "THEN RAISE(ABORT,'owner required') END; END"
        )
    for kind, table in (("agent", "agent_profiles"), ("project", "projects"), ("app", "mini_apps")):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_resource_owner AFTER INSERT ON {table} "  # noqa: S608
            "WHEN access_user_id() IS NOT NULL BEGIN "
            "INSERT OR IGNORE INTO access_resources(kind,resource_id,owner_user_id) "
            f"VALUES('{kind}',NEW.id,access_user_id()); END"
        )


def claim_legacy(user_id: str, conn: sqlite3.Connection | None = None) -> None:
    """Claim ALL pre-account records exactly once, in the bootstrap transaction."""
    if conn is None:
        from .db import db

        with db.transaction() as connection:
            claim_legacy(user_id, connection)
        return
    state = conn.execute("SELECT legacy_owner FROM access_state WHERE id=1").fetchone()
    if state and state[0] is not None:
        return
    for table in OWNED_TABLES:
        conn.execute(f"UPDATE {table} SET owner_user_id=? WHERE owner_user_id IS NULL", (user_id,))  # noqa: S608
    for kind, table, column in (
        ("agent", "agent_profiles", "id"),
        ("project", "projects", "id"),
        ("app", "mini_apps", "id"),
        ("workflow", "workflow_settings", "name"),
    ):
        conn.execute(
            f"INSERT OR IGNORE INTO access_resources(kind,resource_id,owner_user_id) "  # noqa: S608
            f"SELECT ?,{column},? FROM {table}",
            (kind, user_id),
        )
    from .config import settings

    for path in Path(settings.workflows_dir).glob("*.py"):
        if not path.name.startswith("_"):
            conn.execute(
                "INSERT OR IGNORE INTO access_resources VALUES('workflow',?,?)", (path.stem, user_id)
            )
    conn.execute("UPDATE access_state SET legacy_owner=? WHERE id=1", (user_id,))


def require_chat(chat_id: str, who=None) -> dict[str, Any]:
    from .db import db

    who = who or principal()
    row = db.one("SELECT id,owner_user_id FROM chats WHERE id=?", (chat_id,))
    if who is None or who.service or row is None or row["owner_user_id"] != who.user_id:
        raise HTTPException(404, "Chat not found")
    return row


def require_run(workflow_id: str, who=None) -> dict[str, Any]:
    from .db import db

    who = who or principal()
    row = db.one("SELECT * FROM runs WHERE workflow_id=?", (workflow_id,))
    if who is None or who.service or row is None or row["owner_user_id"] != who.user_id:
        raise HTTPException(404, "Run not found")
    return row


def allowed(kind: str, resource_id: str, who=None) -> bool:
    from .db import db

    who = who or principal()
    if who is None or who.service:
        return False
    if who.role == "admin":
        return True  # Workspace administration, never private object access.
    return (
        db.one(
            "SELECT 1 FROM access_grants WHERE kind=? AND resource_id=? AND user_id=?",
            (kind, resource_id, who.user_id),
        )
        is not None
    )


def require_resource(kind: str, resource_id: str, who=None) -> None:
    if not allowed(kind, resource_id, who):
        raise HTTPException(404, "Resource not found")


def visible(kind: str, rows: list[dict], key: str = "id") -> list[dict]:
    return [row for row in rows if allowed(kind, str(row[key]))]


def register_resource(kind: str, resource_id: str, owner_user_id: str | None = None) -> None:
    from .db import db

    owner = owner_user_id or human_id()
    if owner is None:
        state = db.one("SELECT legacy_owner FROM access_state WHERE id=1")
        if state and state["legacy_owner"]:
            raise HTTPException(403, "Explicit resource owner required")
        return
    db.execute("INSERT OR IGNORE INTO access_resources VALUES(?,?,?)", (kind, resource_id, owner))


def require_execution(who=None) -> None:
    """No member may run unbounded agents, Python workflows or Rust/MCP apps.

    Resource visibility grants are deliberately NOT execution capabilities. These
    runtimes share trusted workspace and infrastructure credentials; until they
    can enforce per-user tool/filesystem capabilities, member execution fails
    closed, even for an explicitly granted resource.
    """
    who = who or principal()
    if who is not None and (who.service or who.role != "admin"):
        raise HTTPException(403, "This runtime does not support bounded member execution")


def require_owner_execution(table: str, resource_id: str) -> None:
    """Revalidate durable job ownership even after its originating request ends."""
    from .db import db

    if table not in {"chats", "mini_apps"}:
        raise ValueError("unsupported execution resource")
    row = db.one(f"SELECT owner_user_id FROM {table} WHERE id=?", (resource_id,))  # noqa: S608
    state = db.one("SELECT legacy_owner FROM access_state WHERE id=1")
    if not state or state["legacy_owner"] is None:
        return  # Bootstrap/legacy database, before accounts exist.
    if (
        not row
        or not row["owner_user_id"]
        or not db.one(
            "SELECT id FROM auth_users WHERE id=? AND role='admin' AND status='approved'",
            (row["owner_user_id"],),
        )
    ):
        raise HTTPException(403, "This runtime does not support bounded member execution")


async def authorize_request(request: Request, who) -> None:
    """Called by require_user after authentication; default-deny for members."""
    path, method = request.url.path.rstrip("/"), request.method
    parts = path.split("/")
    # Service-only capability routes perform their own live turn/app checks.
    capability = path in {
        "/api/chat-outputs/prepare",
        "/api/chat-outputs/publish",
        "/api/chat-outputs/read",
        "/api/subagents/list",
        "/api/subagents/call",
        "/api/subagents/wait",
        "/api/mcp-oauth/gate",
        "/api/mcp-oauth/tools",
    }
    if who.service:
        if capability:
            return
        raise HTTPException(403, "Service credential is not a user account")
    if capability:
        raise HTTPException(403, "Service capability required")
    if len(parts) >= 4 and parts[1:3] == ["api", "chats"]:
        require_chat(parts[3], who)
        if who.role != "admin" and method == "PATCH" and len(parts) == 4:
            try:
                fields = await request.json()
            except ValueError as exc:
                raise HTTPException(422, "Invalid chat update") from exc
            if not isinstance(fields, dict) or set(fields) - {"title"}:
                raise HTTPException(403, "Member runtime configuration is not supported")
            return
        if who.role != "admin" and (
            (len(parts) >= 5 and parts[4] in {"messages", "promote", "queue", "title", "project-changes"})
            or (method == "PATCH" and len(parts) == 4)
        ):
            raise HTTPException(403, "Member execution and runtime configuration are not supported")
        return
    if path == "/api/chats" and method in {"GET", "POST"}:
        # Creation validates effective profile/projects before persistence.
        return
    if path == "/api/runs" and method == "GET":
        return
    if len(parts) >= 4 and parts[1:3] == ["api", "runs"]:
        require_run(parts[3], who)
        return
    # Global debugging endpoints reveal private messages/turn arguments even to
    # administrators. A separate audited scoped inspection UI is required.
    if path.startswith("/api/chat-inspection"):
        raise HTTPException(403, "Use the private chat API to inspect your own conversations")
    if path == "/api/search/chats" and method == "GET":
        return
    # Global event streams contain all chats, runs, app operations and tools.
    if path in {"/api/events", "/api/events/recent"}:
        raise HTTPException(403, "Global activity is not a private user stream")
    # Storage inventory/cleanup includes arbitrary private output names and
    # bytes; infrastructure administration is not permission to inspect them.
    if path in {
        "/api/settings/storage/outputs",
        "/api/settings/storage/cleanup/preview",
        "/api/settings/storage/cleanup",
    }:
        raise HTTPException(403, "Cross-user private output inventory is not available")
    if path.startswith("/api/artifacts/"):
        raise HTTPException(403, "Legacy artifacts have no verifiable private owner")
    if path.startswith("/api/drafts"):
        from .db import db

        state = db.one("SELECT legacy_owner FROM access_state WHERE id=1")
        if who.role != "admin" or not state or state["legacy_owner"] != who.user_id:
            raise HTTPException(403, "Legacy workflow drafts belong to the initial administrator")
        return
    if who.role == "admin":
        return
    if path.startswith("/api/access"):
        raise HTTPException(403, "Administrator required")
    # Carefully enumerated read-only shared resources. Collection handlers also
    # filter each row; identifiers never bypass grants.
    for collection, kind in (
        ("agents", "agent"),
        ("projects", "project"),
        ("workflows", "workflow"),
        ("miniapps", "app"),
    ):
        prefix = f"/api/{collection}"
        if path == prefix and method == "GET":
            return
        if path.startswith(prefix + "/"):
            resource_id = parts[3]
            # Never expose collection-level catalog, setup or diagnostic routes.
            if resource_id in {"overview", "contract", "repositories", "github-app", "validate"}:
                break
            require_resource(kind, resource_id, who)
            if method == "GET" and (
                len(parts) == 4 or (collection == "miniapps" and len(parts) == 5 and parts[4] == "status")
            ):
                return
            break
    raise HTTPException(403, "Administrator required")


# Keep schema installation independent of security/auth-store import order.
async def _authenticated(request: Request):
    from .security import require_user

    return await require_user(request)


# Included explicitly by the application's router registry.
router = APIRouter(prefix="/api/access", dependencies=[Depends(_authenticated)])


def _admin() -> None:
    who = principal()
    if who is None or who.service or who.role != "admin":
        raise HTTPException(403, "Administrator required")


@router.get("/grants")
def list_grants() -> dict:
    _admin()
    from .db import db

    return {"grants": db.query("SELECT * FROM access_grants ORDER BY kind,resource_id,user_id")}


@router.put("/grants")
def grant(payload: dict = Body(...)) -> dict:
    _admin()
    from .db import db

    kind, resource_id, user_id = (payload.get(key) for key in ("kind", "resource_id", "user_id"))
    if (
        kind not in KINDS
        or not isinstance(resource_id, str)
        or not resource_id
        or not isinstance(user_id, str)
    ):
        raise HTTPException(422, "Provide kind, resource_id and user_id")
    if not db.one("SELECT id FROM auth_users WHERE id=? AND status='approved'", (user_id,)):
        raise HTTPException(404, "Approved user not found")
    db.execute(
        "INSERT OR IGNORE INTO access_grants VALUES(?,?,?,?)", (kind, resource_id, user_id, time.time())
    )
    return {"ok": True}


@router.delete("/grants")
def revoke(payload: dict = Body(...)) -> dict:
    _admin()
    from .db import db

    db.execute(
        "DELETE FROM access_grants WHERE kind=? AND resource_id=? AND user_id=?",
        tuple(payload.get(key) for key in ("kind", "resource_id", "user_id")),
    )
    return {"ok": True}
