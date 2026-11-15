"""Application-level private ownership and explicit workspace grants.

Private objects are never made public by an administrator role. Service tokens
are transport credentials, not administrator accounts. New routes are denied to
members until reviewed here; sharing a saved runtime never grants account administration.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

KINDS = frozenset({"agent", "project", "workflow", "app", "tool", "repository"})
OWNED_TABLES = {"chats": "id", "runs": "id", "mini_apps": "id", "workflow_settings": "name"}
execution_scope: ContextVar[tuple[str, str] | None] = ContextVar("execution_scope", default=None)
_delegations: dict[str, Any] = {}
_activity_delegations: dict[str, dict[str, Any]] = {}


def issue_activity_grant(user_id: str, run_id: str, tools: list[str] | None, ttl: int = 900) -> str:
    now = time.time()
    for key in [key for key, value in _activity_delegations.items() if value["expires_at"] <= now]:
        _activity_delegations.pop(key, None)
    token = secrets.token_urlsafe(32)
    _activity_delegations[token] = {
        "user_id": user_id,
        "run_id": run_id,
        "tools": tools,
        "expires_at": now + min(max(ttl, 1), 3600),
    }
    return token


def release_activity_grant(token: str) -> None:
    _activity_delegations.pop(token, None)


@contextmanager
def activity_grant(user_id: str | None, run_id: str, tools: list[str] | None, ttl: int = 900):
    token = issue_activity_grant(user_id, run_id, tools, ttl) if user_id else None
    try:
        yield {"grant": token} if token else None
    finally:
        if token:
            release_activity_grant(token)


def delegate(who, *, tools: list[str] | None = None) -> str:
    token = secrets.token_urlsafe(32)
    _delegations[token] = {"principal": who, "tools": tools}
    return token


def release_delegation(token: str) -> None:
    _delegations.pop(token, None)


def authorize_actor_tool(token: str, tool: str) -> None:
    if token in _delegations:
        selected = _delegations[token]["tools"]
        if selected is not None and tool not in selected:
            raise HTTPException(403, "This tool is not enabled for this delegated operation")
        return
    if token in _activity_delegations:
        selected = _activity_delegations[token]["tools"]
        if selected is not None and tool not in selected:
            raise HTTPException(403, "This tool is not enabled for this activity")
        return
    from .chat_outputs import grant_scope
    from .db import db

    chat_id, turn_id = grant_scope(token)
    row = db.one("SELECT job FROM chat_turns WHERE id=? AND chat_id=?", (turn_id, chat_id))
    if not row or not row["job"]:
        raise HTTPException(403, "This agent has no durable tool authorization")
    selected = json.loads(row["job"]).get("tools")
    if selected is not None and tool not in selected:
        raise HTTPException(403, "This tool is not enabled for this agent turn")


def actor_for_grant(token: str):
    from .chat_outputs import grant_scope
    from .db import db
    from .security import Principal

    if not isinstance(token, str) or not token or len(token) > 128:
        raise HTTPException(401, "Invalid delegated capability")
    activity = _activity_delegations.get(token)
    if activity is not None:
        user = db.one("SELECT id,role,status FROM auth_users WHERE id=?", (activity["user_id"],))
        run = (
            db.one(
                "SELECT status,owner_user_id FROM runs WHERE workflow_id=? OR run_id=? LIMIT 1",
                (activity["run_id"], activity["run_id"]),
            )
            if activity["run_id"]
            else None
        )
        if (
            activity["expires_at"] <= time.time()
            or not user
            or user["status"] != "active"
            or (run and (run["status"] != "running" or run["owner_user_id"] != user["id"]))
        ):
            _activity_delegations.pop(token, None)
            raise HTTPException(403, "The workflow activity capability expired")
        return Principal(user["id"], user["role"])
    delegation = _delegations.get(token)
    if delegation is not None:
        from .account_streams import live

        delegated = delegation["principal"]
        if not live(delegated):
            raise HTTPException(403, "The delegated session expired")
        user = db.one("SELECT role FROM auth_users WHERE id=?", (delegated.user_id,))
        return Principal(delegated.user_id, user["role"], delegated.session_id)
    chat_id, turn_id = grant_scope(token)
    row = db.one(
        "SELECT c.owner_user_id,u.role FROM chats c JOIN auth_users u ON u.id=c.owner_user_id "
        "JOIN chat_turns t ON t.chat_id=c.id WHERE c.id=? AND t.id=? "
        "AND t.state='running' AND t.stop_requested=0 "
        "AND u.status='active'",
        (chat_id, turn_id),
    )
    if not row:
        raise HTTPException(403, "This delegated account capability has expired")
    from .subagents import ancestor_stopped

    if ancestor_stopped(turn_id):
        raise HTTPException(403, "The delegated parent turn has stopped")
    require_owner_execution("chats", chat_id)
    return Principal(row["owner_user_id"], row["role"])


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
        "CREATE TABLE IF NOT EXISTS access_revocations (user_id TEXT PRIMARY KEY,created_at REAL NOT NULL)",
        "INSERT OR IGNORE INTO access_state(id) VALUES(1)",
        "CREATE TABLE IF NOT EXISTS access_resources (kind TEXT NOT NULL, resource_id TEXT NOT NULL, "
        "owner_user_id TEXT NOT NULL, PRIMARY KEY(kind,resource_id))",
        "CREATE TABLE IF NOT EXISTS access_legacy_artifacts "
        "(path TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS access_workflow_chats (workflow TEXT NOT NULL,user_id TEXT NOT NULL,"
        "chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,PRIMARY KEY(workflow,user_id))",
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
    root = Path(settings.artifacts_dir)
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                conn.execute(
                    "INSERT OR IGNORE INTO access_legacy_artifacts VALUES(?,?)",
                    (path.relative_to(root).as_posix(), user_id),
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
            who = principal()
            if who is None or not who.service:
                raise HTTPException(403, "Explicit resource owner required")
            # Trusted internal authoring installs workspace resources for the
            # initial workspace owner. It remains a service actor, not a human.
            owner = state["legacy_owner"]
        else:
            return
    db.execute("INSERT OR IGNORE INTO access_resources VALUES(?,?,?)", (kind, resource_id, owner))


def member_tools(selection: list[str] | None, user_id: str) -> list[str]:
    from .db import db

    shared = {
        row["resource_id"]
        for row in db.query(
            "SELECT resource_id FROM access_grants WHERE kind='tool' AND user_id=?", (user_id,)
        )
    }
    return sorted(shared if selection is None else shared.intersection(selection))


def require_execution(who=None) -> None:
    """Execution is deliberate sharing inside a trusted, not hostile-tenant workspace."""
    who = who or principal()
    if who is None or who.role == "admin":
        return
    if who.service:
        # Authenticated internal authoring/worker services are explicit automation
        # principals. They never gain private chat/account HTTP access.
        return
    scope = execution_scope.get()
    if scope is None or not allowed(*scope, who):
        raise HTTPException(403, "An administrator must grant access to this runtime")


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
            "SELECT id FROM auth_users WHERE id=? AND status='active'",
            (row["owner_user_id"],),
        )
    ):
        raise HTTPException(403, "The runtime owner is unavailable")
    if table == "chats":
        owner = db.one("SELECT role FROM auth_users WHERE id=?", (row["owner_user_id"],))
        if owner["role"] != "admin":
            chat = db.one("SELECT agent_id FROM chats WHERE id=?", (resource_id,))
            if not chat["agent_id"] or not db.one(
                "SELECT 1 FROM access_grants WHERE kind='agent' AND resource_id=? AND user_id=?",
                (chat["agent_id"], row["owner_user_id"]),
            ):
                raise HTTPException(403, "This agent is no longer shared with the chat owner")
            projects = db.one("SELECT project_ids,tools,packages FROM chats WHERE id=?", (resource_id,))
            if projects["tools"] is None or json.loads(projects["packages"] or "[]"):
                raise HTTPException(403, "Start a new chat using a shared agent to use member permissions")
            for tool in json.loads(projects["tools"]):
                if not db.one(
                    "SELECT 1 FROM access_grants WHERE kind='tool' AND resource_id=? AND user_id=?",
                    (tool, row["owner_user_id"]),
                ):
                    raise HTTPException(403, "A selected tool is no longer shared with the chat owner")
            for project_id in json.loads(projects["project_ids"] or "[]"):
                if not db.one(
                    "SELECT 1 FROM access_grants WHERE kind='project' AND resource_id=? AND user_id=?",
                    (project_id, row["owner_user_id"]),
                ):
                    raise HTTPException(403, "A selected project is no longer shared with the chat owner")


async def suspend_owned_work(user_id: str) -> bool:
    from .clients import temporal
    from .db import db

    success = True
    for row in db.query(
        "SELECT resource_id FROM access_resources WHERE kind='workflow' AND owner_user_id=?", (user_id,)
    ):
        name = row["resource_id"]
        db.execute(
            "INSERT INTO workflow_settings(name,disabled,owner_user_id) VALUES(?,1,?) "
            "ON CONFLICT(name) DO UPDATE SET disabled=1",
            (name, user_id),
        )
        try:
            await temporal.set_workflow_disabled(name, True)
        except Exception:  # noqa: BLE001 - persisted suspension is retried by maintenance.
            success = False
    for row in db.query(
        "SELECT workflow_id FROM runs WHERE owner_user_id=? AND status='running'", (user_id,)
    ):
        try:
            await temporal.cancel(row["workflow_id"])
        except Exception:  # noqa: BLE001 - do not expose private run errors to account administration.
            success = False
    db.execute(
        "UPDATE chat_turns SET stop_requested=1 WHERE state IN ('running','queued') "
        "AND chat_id IN (SELECT id FROM chats WHERE owner_user_id=?)",
        (user_id,),
    )
    if success:
        db.execute("DELETE FROM access_revocations WHERE user_id=?", (user_id,))
    return success


async def account_maintenance() -> None:
    from .db import db

    while True:
        for row in db.query(
            "SELECT id FROM auth_users WHERE status='suspended' UNION "
            "SELECT user_id AS id FROM access_revocations"
        ):
            try:
                await suspend_owned_work(row["id"])
            except Exception:  # noqa: BLE001 - retry durable account suspension after infrastructure failures.
                import logging

                logging.getLogger(__name__).warning("Account automation suspension is pending retry")
        await asyncio.sleep(15)


async def authorize_request(request: Request, who) -> None:
    """Called by require_user after authentication; default-deny for members."""
    path, method = request.url.path.rstrip("/"), request.method
    parts = path.split("/")
    execution_scope.set(None)
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
        # Human callers may use only their own unforgeable turn capabilities.
        # Endpoint schemas and live-turn checks remain authoritative for malformed
        # or expired capabilities, preserving their safe validation responses.
        try:
            body = await request.json()
        except (ValueError, RuntimeError):
            if who.role != "admin":
                raise HTTPException(403, "A private turn capability is required") from None
            return
        grant_token = body.get("grant") if isinstance(body, dict) else None
        if grant_token:
            from .chat_outputs import grant_scope

            chat_id, _ = grant_scope(grant_token)
            require_chat(chat_id, who)
        elif who.role != "admin":
            raise HTTPException(403, "A private turn capability is required")
        return
    if len(parts) >= 4 and parts[1:3] == ["api", "chats"]:
        owned_chat = require_chat(parts[3], who)
        if who.role != "admin":
            from .db import db

            chat = db.one("SELECT agent_id FROM chats WHERE id=?", (owned_chat["id"],))
            if chat and chat["agent_id"]:
                execution_scope.set(("agent", chat["agent_id"]))
        if who.role != "admin" and method == "PATCH" and len(parts) == 4:
            try:
                fields = await request.json()
            except ValueError as exc:
                raise HTTPException(422, "Invalid chat update") from exc
            if not isinstance(fields, dict) or set(fields) - {"title"}:
                raise HTTPException(403, "Member runtime configuration is not supported")
            return
        if who.role != "admin" and len(parts) >= 5:
            if parts[4] in {"promote", "project-changes"}:
                raise HTTPException(403, "Administrator required")
            if parts[4] in {"messages", "queue", "title", "branch"} and method not in {"GET", "HEAD"}:
                require_owner_execution("chats", parts[3])
        return
    if path.startswith("/api/tokens"):
        return  # Key metadata/history handlers enforce ownership, even for admins.
    if path.startswith("/api/triggers/") or (
        path.startswith("/api/workflows/") and path.endswith("/webhook-token")
    ):
        require_resource("workflow", parts[3], who)
        execution_scope.set(("workflow", parts[3]))
        return
    if path in {"/api/settings/usage", "/api/settings/usage/export"} and method == "GET":
        return  # Session-only analytics; the service constrains every member projection.
    if path == "/api/catalog" and method == "GET":
        return
    if path.startswith("/api/speech") and method in {"GET", "POST"}:
        return  # Enabled local dictation is a workspace capability; tickets are single-use.
    if path == "/api/chats" and method in {"GET", "POST"}:
        # Creation validates effective profile/projects before persistence.
        return
    if path == "/api/runs" and method == "GET":
        return
    if len(parts) >= 4 and parts[1:3] == ["api", "runs"]:
        require_run(parts[3], who)
        return
    # Diagnostics remain useful but every query is constrained to the acting
    # account, including administrators and delegated agent requests.
    if path.startswith("/api/chat-inspection"):
        return  # Every diagnostic query is constrained by account ownership.
    if path == "/api/search/chats" and method == "GET":
        return
    # The underlying event bus is global; account_streams projects a private view.
    if path in {"/api/events", "/api/events/recent"}:
        return  # The event transport filters every record by private ownership.
    # Storage inventory/cleanup includes arbitrary private output names and
    # bytes; infrastructure administration is not permission to inspect them.
    if path in {
        "/api/settings/storage/outputs",
        "/api/settings/storage/cleanup/preview",
        "/api/settings/storage/cleanup",
    }:
        if who.role != "admin":
            raise HTTPException(403, "Administrator required")
        return  # Inventory and every deletion are constrained to this account.
    if path.startswith("/api/artifacts/"):
        from .db import db

        relative = path.removeprefix("/api/artifacts/")
        if not db.one(
            "SELECT 1 FROM access_legacy_artifacts WHERE path=? AND owner_user_id=?", (relative, who.user_id)
        ):
            raise HTTPException(404, "Artifact not found")
        return
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
            if (
                collection in {"workflows", "miniapps"}
                and len(parts) == 5
                and parts[4] in {"run", "invoke", "request"}
            ):
                execution_scope.set((kind, resource_id))
                return
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


def _admin(request: Request | None = None) -> None:
    who = principal()
    if who is None or who.service or who.role != "admin":
        raise HTTPException(403, "Administrator required")
    if request is not None:
        from .security import require_recent

        require_recent(request)


@router.get("/resources")
def list_resources() -> dict:
    _admin()
    from .db import db

    resources = db.query("SELECT * FROM access_resources ORDER BY kind,resource_id")
    for row in resources:
        kind, key = row["kind"], row["resource_id"]
        if kind == "agent":
            named = db.one("SELECT name FROM agent_profiles WHERE id=?", (key,))
        elif kind == "app":
            named = db.one("SELECT title AS name FROM mini_apps WHERE id=?", (key,))
        elif kind == "project":
            named = db.one("SELECT full_name AS name FROM projects WHERE id=?", (key,))
        else:
            named = None
        row["name"] = named["name"] if named else key
    return {"resources": resources}


@router.get("/grants")
def list_grants() -> dict:
    _admin()
    from .db import db

    return {"grants": db.query("SELECT * FROM access_grants ORDER BY kind,resource_id,user_id")}


@router.put("/grants")
def grant(request: Request, payload: dict = Body(...)) -> dict:
    _admin(request)
    from .db import db

    kind, resource_id, user_id = (payload.get(key) for key in ("kind", "resource_id", "user_id"))
    if (
        kind not in KINDS
        or not isinstance(resource_id, str)
        or not resource_id
        or len(resource_id) > 256
        or not isinstance(user_id, str)
        or len(user_id) > 64
    ):
        raise HTTPException(422, "Provide kind, resource_id and user_id")
    if not db.one("SELECT id FROM auth_users WHERE id=? AND status='active'", (user_id,)):
        raise HTTPException(404, "Active user not found")
    if kind in {"agent", "project", "workflow", "app"} and not db.one(
        "SELECT 1 FROM access_resources WHERE kind=? AND resource_id=?", (kind, resource_id)
    ):
        raise HTTPException(404, "Resource not found")
    from .auth_store import audit

    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO access_grants VALUES(?,?,?,?)", (kind, resource_id, user_id, time.time())
        )
        audit(conn, "resource.shared", human_id(), user_id, f"{kind}:{resource_id}")
    return {"ok": True}


@router.delete("/grants")
def revoke(request: Request, payload: dict = Body(...)) -> dict:
    _admin(request)
    from .auth_store import audit
    from .db import db

    values = tuple(payload.get(key) for key in ("kind", "resource_id", "user_id"))
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in values):
        raise HTTPException(422, "Provide kind, resource_id and user_id")
    with db.transaction() as conn:
        conn.execute("DELETE FROM access_grants WHERE kind=? AND resource_id=? AND user_id=?", values)
        audit(conn, "resource.unshared", human_id(), values[2], f"{values[0]}:{values[1]}")
    return {"ok": True}
