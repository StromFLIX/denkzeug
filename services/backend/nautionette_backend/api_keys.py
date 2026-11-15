"""Opaque scoped keys and durable, secret-free request auditing.

Keys are never sessions. Only hashes are persisted, permissions are intersected
with the live owning account on every request, and grants never imply delegation.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from contextvars import ContextVar

from fastapi import HTTPException, Request

from .api_policy import SCOPES, Policy
from .auth_store import audit, digest, identifier, secret, store, throttle

DEFAULT_DAYS = 90
MAX_DAYS = 365
WEBHOOK_DAYS = 7
DETAIL_DAYS = 90
AGGREGATE_DAYS = 365
MEMBER_SCOPES = frozenset(
    {
        "workflows:read",
        "workflows:trigger",
        "runs:read",
        "runs:cancel",
        "runs:terminate",
        "chats:read",
        "chats:write",
        "chats:delete",
        "agents:read",
        "projects:read",
        "miniapps:read",
        "miniapps:invoke",
        "outputs:read",
        "mcp:read",
        "tokens:read",
    }
)
# Only carried within an external MCP request and its in-process HTTP dispatch.
transport_secret: ContextVar[str | None] = ContextVar("api_key_transport", default=None)
transport_tool: ContextVar[str | None] = ContextVar("api_key_tool", default=None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, prefix TEXT NOT NULL, secret_hash TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind IN ('api_key','webhook')), grants TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL, last_used_at REAL,
 replacement_for TEXT, total_requests INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS api_keys_owner ON api_keys(owner_user_id,created_at);
CREATE TABLE IF NOT EXISTS api_resource_ids (
 kind TEXT NOT NULL, name TEXT NOT NULL, id TEXT NOT NULL UNIQUE, PRIMARY KEY(kind,name)
);
CREATE TABLE IF NOT EXISTS api_key_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
 request_id TEXT NOT NULL, created_at REAL NOT NULL, method TEXT NOT NULL, route TEXT NOT NULL,
 resource_id TEXT, required_scope TEXT, status INTEGER NOT NULL DEFAULT 0,
 duration_ms REAL NOT NULL DEFAULT 0, outcome TEXT NOT NULL DEFAULT 'started', tool TEXT, run_id TEXT
);
CREATE INDEX IF NOT EXISTS api_key_requests_token ON api_key_requests(token_id,id DESC);
CREATE INDEX IF NOT EXISTS api_key_requests_age ON api_key_requests(created_at);
CREATE TABLE IF NOT EXISTS api_key_daily (
 token_id TEXT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE, day INTEGER NOT NULL,
 requests INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(token_id,day)
);
"""


def workflow_identity(name: str) -> str:
    """Generation ID, never a re-usable workflow name. Invalidated on deletion."""
    with store().transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO api_resource_ids(kind,name,id) VALUES('workflow',?,?)",
            (name, identifier()),
        )
        return conn.execute(
            "SELECT id FROM api_resource_ids WHERE kind='workflow' AND name=?", (name,)
        ).fetchone()[0]


def invalidate_workflow(name: str) -> None:
    store().execute("DELETE FROM api_resource_ids WHERE kind='workflow' AND name=?", (name,))


def public_key(row: dict) -> dict:
    return {
        **{
            key: row.get(key)
            for key in (
                "id",
                "name",
                "prefix",
                "kind",
                "created_at",
                "expires_at",
                "revoked_at",
                "last_used_at",
                "requests_7d",
                "failures_7d",
                "total_requests",
            )
        },
        "grants": json.loads(row["grants"]),
        "requests_7d": row.get("requests_7d") or 0,
        "failures_7d": row.get("failures_7d") or 0,
        "total_requests": row.get("total_requests") or 0,
    }


def owned_key(token_id: str, owner: str) -> dict:
    row = store().one("SELECT * FROM api_keys WHERE id=? AND owner_user_id=?", (token_id, owner))
    if not row:
        raise HTTPException(404, "API key not found")
    return row


def validate_grants(grants: list, who) -> list:
    from . import access

    if not isinstance(grants, list) or not 1 <= len(grants) <= len(SCOPES):
        raise HTTPException(422, "Choose at least one permission")
    result, seen = [], set()
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) - {"scope", "resources"}:
            raise HTTPException(422, "Invalid permission grant")
        scope, resources = grant.get("scope"), grant.get("resources")
        if not isinstance(scope, str) or scope not in SCOPES or scope in seen:
            raise HTTPException(422, "Unknown or duplicate permission")
        if who.role != "admin" and scope not in MEMBER_SCOPES:
            raise HTTPException(403, "You cannot delegate this permission")
        kind = SCOPES[scope]["resource_kind"]
        if resources is not None:
            if not kind or not isinstance(resources, list) or not 1 <= len(resources) <= 100:
                raise HTTPException(422, "Invalid resource restriction")
            if any(not isinstance(value, str) or not value or len(value) > 200 for value in resources):
                raise HTTPException(422, "Invalid resource identifier")
            resources = sorted(set(resources))
            for value in resources:
                if kind == "workflow":
                    identity = store().one(
                        "SELECT name FROM api_resource_ids WHERE kind='workflow' AND id=?", (value,)
                    )
                    if not identity:
                        raise HTTPException(404, "Workflow not found")
                    access.require_resource("workflow", identity["name"], who)
                elif kind == "chat":
                    access.require_chat(value, who)
                elif kind == "run":
                    access.require_run(value, who)
                elif kind in {"agent", "project", "app", "tool"}:
                    access.require_resource(kind, value, who)
                elif kind == "output":
                    if not store().one(
                        "SELECT 1 FROM chat_outputs o JOIN chats c ON c.id=o.chat_id "
                        "WHERE o.id=? AND c.owner_user_id=?",
                        (value, who.user_id),
                    ):
                        raise HTTPException(404, "Output not found")
        seen.add(scope)
        result.append({"scope": scope, "resources": resources})
    return result


def issue(
    who,
    name: str,
    grants: list,
    expires_at: float | None = None,
    *,
    kind="api_key",
    replacement_for=None,
    conn=None,
    validated=False,
) -> dict:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or any(ord(c) < 32 for c in name):
        raise HTTPException(422, "Give this key a name of 1–100 characters")
    now = time.time()
    expires_at = now + DEFAULT_DAYS * 86400 if expires_at is None else expires_at
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or not math.isfinite(expires_at)
        or not now < expires_at <= now + MAX_DAYS * 86400
    ):
        raise HTTPException(422, "Expiry must be in the future and within one year")
    if not validated:
        grants = validate_grants(grants, who)
    raw = "dz_wht_" + secret() if kind == "webhook" else "dz_key_" + secret()
    row = {
        "id": identifier(),
        "owner_user_id": who.user_id,
        "name": name.strip(),
        "prefix": raw[:15],
        "kind": kind,
        "grants": json.dumps(grants),
        "created_at": now,
        "expires_at": expires_at,
        "replacement_for": replacement_for,
    }

    def insert(connection):
        user = connection.execute(
            "SELECT role,status,must_change_password FROM auth_users WHERE id=?", (who.user_id,)
        ).fetchone()
        if not user or user["status"] != "active" or user["must_change_password"] or user["role"] != who.role:
            raise HTTPException(403, "Account permissions changed; sign in again before issuing a key")
        connection.execute(
            "INSERT INTO api_keys(id,owner_user_id,name,prefix,secret_hash,kind,grants,created_at,"
            "expires_at,replacement_for) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                who.user_id,
                row["name"],
                row["prefix"],
                digest(raw),
                kind,
                row["grants"],
                now,
                expires_at,
                replacement_for,
            ),
        )
        audit(connection, "api_key.created", who.user_id, who.user_id, row["id"])

    if conn is not None:
        insert(conn)
    else:
        with store().transaction() as connection:
            insert(connection)
    return {"token": public_key(row), "secret": raw}


def observe(raw: str, request: Request) -> dict | None:
    """Attribute attempts before authentication or payload admission; no secret logging."""
    if len(raw) > 512:
        return None
    row = store().one(
        "SELECT k.*,u.role,u.status,u.must_change_password FROM api_keys k "
        "JOIN auth_users u ON u.id=k.owner_user_id WHERE secret_hash=?",
        (digest(raw),),
    )
    if row:
        request.state.api_key = row
    return row


def identify(raw: str, request: Request) -> dict:
    row = observe(raw, request)
    if row is None:
        throttle("api-key-failure:" + (request.client.host if request.client else "local"), 60, window=60)
        with store().transaction() as conn:
            audit(conn, "api_key.authentication_failed")
    if (
        not row
        or row["revoked_at"] is not None
        or row["expires_at"] <= time.time()
        or row["status"] != "active"
        or row["must_change_password"]
    ):
        raise HTTPException(401, "API key expired, revoked, or unavailable")
    return row


def authorize_key(request: Request, row: dict, policy: Policy) -> None:
    if policy.authentication not in {"account", "mcp"}:
        raise HTTPException(403, "This credential is not accepted for this operation")
    kind, resource_id = None, None
    if policy.resource:
        kind, param = policy.resource.split(":", 1)
        resource_id = request.state.api_params.get(param)
        if kind == "workflow":
            # Do not create an identity during authorization of an arbitrary URL.
            value = store().one(
                "SELECT id FROM api_resource_ids WHERE kind='workflow' AND name=?", (resource_id,)
            )
            resource_id = value["id"] if value else None
    request.state.api_resource_id = resource_id
    for grant in json.loads(row["grants"]):
        if grant["scope"] != policy.scope:
            continue
        resources = grant["resources"]
        if resources is None or (
            kind == SCOPES[policy.scope]["resource_kind"]
            and resource_id is not None
            and resource_id in resources
        ):
            if row["kind"] == "webhook" and (
                request.method != "POST" or request.state.api_route != "/api/triggers/{name}"
            ):
                raise HTTPException(403, "Webhook keys may only trigger their workflow")
            return
    raise HTTPException(403, "API key lacks the required scope or resource permission")


def begin_request(request: Request) -> int | None:
    row = getattr(request.state, "api_key", None)
    if not row:
        return None
    policy = getattr(request.state, "api_policy", None)
    now = time.time()
    with store().transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO api_key_requests(token_id,request_id,created_at,method,route,resource_id,"
            "required_scope,tool) VALUES(?,?,?,?,?,?,?,?)",
            (
                row["id"],
                identifier(),
                now,
                request.method,
                getattr(request.state, "api_route", "unmatched"),
                getattr(request.state, "api_resource_id", None),
                policy.scope if policy else None,
                transport_tool.get(),
            ),
        )
        conn.execute(
            "UPDATE api_keys SET last_used_at=?,total_requests=total_requests+1 WHERE id=?", (now, row["id"])
        )
        conn.execute(
            "INSERT INTO api_key_daily(token_id,day,requests) VALUES(?,?,1) "
            "ON CONFLICT(token_id,day) DO UPDATE SET requests=requests+1",
            (row["id"], int(now // 86400) * 86400),
        )
        return cursor.lastrowid


def finish_request(request_id: int | None, status: int, elapsed: float, request: Request) -> None:
    if request_id is None:
        return
    outcome = getattr(request.state, "api_rpc_outcome", None) or (
        "denied" if status in {401, 403} else "failed" if status >= 400 else "succeeded"
    )
    policy = getattr(request.state, "api_policy", None)
    with store().transaction() as conn:
        row = conn.execute(
            "SELECT token_id,created_at,outcome FROM api_key_requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row or row["outcome"] != "started":
            return
        conn.execute(
            "UPDATE api_key_requests SET status=?,duration_ms=?,outcome=?,run_id=?,"
            "required_scope=?,resource_id=?,tool=COALESCE(?,tool) WHERE id=?",
            (
                status,
                round(elapsed * 1000, 2),
                outcome,
                getattr(request.state, "api_run_id", None),
                policy.scope if policy else None,
                getattr(request.state, "api_resource_id", None),
                getattr(request.state, "api_tool", None),
                request_id,
            ),
        )
        if outcome in {"denied", "failed"}:
            conn.execute(
                "UPDATE api_key_daily SET failures=failures+1 WHERE token_id=? AND day=?",
                (row["token_id"], int(row["created_at"] // 86400) * 86400),
            )


async def maintenance() -> None:
    while True:
        store().execute(
            "DELETE FROM api_key_requests WHERE created_at<?", (time.time() - DETAIL_DAYS * 86400,)
        )
        store().execute("DELETE FROM api_key_daily WHERE day<?", (time.time() - AGGREGATE_DAYS * 86400,))
        await asyncio.sleep(3600)


def webhook_limit(row: dict) -> None:
    throttle("workflow-webhook:" + row["id"], 60, window=60)
