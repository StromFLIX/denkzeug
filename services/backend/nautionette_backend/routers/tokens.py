"""Account-owned API keys. Secrets are returned once and never redisplayed."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import access, api_keys
from ..api_policy import SCOPES
from ..auth_store import audit, store, throttle
from ..clients import authoring
from ..config import settings
from ..security import require_user
from ..workflow_control import workflow_lock

router = APIRouter(dependencies=[Depends(require_user)], tags=["API keys"])


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    expires_at: float | None = None
    grants: list[dict[str, Any]] = Field(min_length=1, max_length=100)


class WebhookCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int | None = Field(default=7, ge=1, le=30, strict=True)


def who(request):
    principal = request.state.principal
    if principal.service:
        raise HTTPException(403, "User account required")
    return principal


async def workflow_definition(name: str):
    try:
        return await authoring.get_workflow(name)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise HTTPException(404, "Workflow not found") from error
        raise HTTPException(503, "Workflow authoring is unavailable; try again when it is ready") from error
    except httpx.TransportError as error:
        raise HTTPException(503, "Workflow authoring is unavailable; try again when it is ready") from error


@router.get("/api/tokens/catalog")
async def catalog(request: Request):
    principal = who(request)
    resources = []
    warnings = []
    try:
        # Resource discovery must not prevent emergency key revocation during
        # an authoring outage, nor assign an ID from a stale deleted inventory.
        async with asyncio.timeout(3):
            for workflow in await authoring.list_workflows():
                name = workflow["name"]
                if not access.allowed("workflow", name, principal):
                    continue
                async with workflow_lock(name):
                    try:
                        await workflow_definition(name)
                    except HTTPException as error:
                        if error.status_code != 404:
                            raise
                        continue
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code != 404:
                            raise
                        continue
                    resources.append(
                        {"kind": "workflow", "id": api_keys.workflow_identity(name), "name": name}
                    )
    except (TimeoutError, HTTPException, httpx.HTTPError):
        warnings.append("Workflow resource lookup is unavailable. Existing keys can still be managed.")
    for row in store().query("SELECT * FROM access_resources WHERE kind IN ('agent','project','app')"):
        if access.allowed(row["kind"], row["resource_id"], principal):
            table, label = {
                "agent": ("agent_profiles", "name"),
                "project": ("projects", "full_name"),
                "app": ("mini_apps", "title"),
            }[row["kind"]]
            named = store().one(f"SELECT {label} AS name FROM {table} WHERE id=?", (row["resource_id"],))  # noqa: S608
            resources.append(
                {
                    "kind": row["kind"],
                    "id": row["resource_id"],
                    "name": named["name"] if named else row["resource_id"],
                }
            )
    from ..api_policy import POLICIES
    from ..main import backend_mcp

    if principal.role == "admin":
        for tool in backend_mcp.tools:
            operation = backend_mcp.operations[tool.name]
            if POLICIES[f"{operation['method']} {operation['path']}"].authentication == "account":
                resources.append({"kind": "tool", "id": tool.name, "name": tool.name.replace("_", " ")})
    for row in store().query(
        "SELECT o.id,r.manifest FROM chat_outputs o JOIN chats c ON c.id=o.chat_id "
        "JOIN chat_output_revisions r ON r.output_id=o.id AND r.revision=o.current_revision "
        "WHERE c.owner_user_id=?",
        (principal.user_id,),
    ):
        resources.append(
            {"kind": "output", "id": row["id"], "name": json.loads(row["manifest"]).get("title", row["id"])}
        )
    for row in store().query("SELECT id,title FROM chats WHERE owner_user_id=?", (principal.user_id,)):
        resources.append({"kind": "chat", "id": row["id"], "name": row["title"] or row["id"]})
    for row in store().query(
        "SELECT workflow_id,workflow FROM runs WHERE owner_user_id=?", (principal.user_id,)
    ):
        resources.append({"kind": "run", "id": row["workflow_id"], "name": row["workflow"]})
    return {
        "scopes": [
            value
            for scope, value in SCOPES.items()
            if (principal.role == "admin" or scope in api_keys.MEMBER_SCOPES)
            and (
                scope in {"mcp:read", "mcp:execute"}
                or any(
                    policy.scope == scope and policy.authentication == "account"
                    for policy in POLICIES.values()
                )
            )
        ],
        "resources": resources,
        "warnings": warnings,
        "policy": {
            "default_days": api_keys.DEFAULT_DAYS,
            "max_days": api_keys.MAX_DAYS,
            "webhook_days": api_keys.WEBHOOK_DAYS,
            "allow_never_expire": True,
        },
    }


@router.get("/api/tokens")
def list_tokens(request: Request):
    principal = who(request)
    rows = store().query(
        "SELECT k.*,COALESCE(SUM(d.requests),0) AS requests_7d,COALESCE(SUM(d.failures),0) AS failures_7d "
        "FROM api_keys k LEFT JOIN api_key_daily d ON d.token_id=k.id AND d.day>=? "
        "WHERE k.owner_user_id=? GROUP BY k.id ORDER BY k.created_at DESC",
        (int(time.time() // 86400 - 6) * 86400, principal.user_id),
    )
    return {"tokens": [api_keys.public_key(row) for row in rows]}


@router.post("/api/tokens", status_code=201)
def create_token(request: Request, body: KeyCreate):
    principal = who(request)
    throttle("api-key-create:" + principal.user_id, 30, window=3600)
    expiry = body.expires_at if "expires_at" in body.model_fields_set else api_keys.DEFAULT_EXPIRY
    return api_keys.issue(principal, body.name, body.grants, expiry)


@router.delete("/api/tokens/{token_id}")
def revoke_token(token_id: str, request: Request):
    principal = who(request)
    api_keys.owned_key(token_id, principal.user_id)
    with store().transaction() as conn:
        conn.execute(
            "UPDATE api_keys SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (time.time(), token_id)
        )
        audit(conn, "api_key.revoked", principal.user_id, principal.user_id, token_id)
    return {"ok": True}


@router.post("/api/tokens/{token_id}/rotate", status_code=201)
def rotate_token(token_id: str, request: Request):
    principal = who(request)
    throttle("api-key-create:" + principal.user_id, 30, window=3600)
    original = api_keys.owned_key(token_id, principal.user_id)
    grants = api_keys.validate_grants(json.loads(original["grants"]), principal)
    with store().transaction() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE id=? AND owner_user_id=?", (token_id, principal.user_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "API key not found")
        if row["revoked_at"] is not None:
            raise HTTPException(409, "This key has already been revoked")
        if row["expires_at"] is not None and row["expires_at"] <= time.time():
            raise HTTPException(409, "Create a new key instead of rotating an expired key")
        result = api_keys.issue(
            principal,
            row["name"],
            grants,
            row["expires_at"],
            kind=row["kind"],
            replacement_for=token_id,
            conn=conn,
            validated=True,
        )
        conn.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (time.time(), token_id))
        audit(conn, "api_key.rotated", principal.user_id, principal.user_id, token_id)
    if result["token"]["kind"] == "webhook" and settings.public_url:
        identity = grants[0]["resources"][0]
        workflow = store().one(
            "SELECT name FROM api_resource_ids WHERE kind='workflow' AND id=?", (identity,)
        )
        if workflow:
            result["url"] = (
                f"{settings.public_url.rstrip('/')}/api/triggers/{quote(workflow['name'], safe='')}"
                f"?token={quote(result['secret'], safe='')}"
            )
    return result


@router.get("/api/tokens/{token_id}/requests")
def requests(
    token_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    api_keys.owned_key(token_id, who(request).user_id)
    rows = store().query(
        "SELECT id,request_id,created_at,method,route,resource_id,required_scope,status,duration_ms,"
        "outcome,tool,run_id FROM api_key_requests WHERE token_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (token_id, limit, offset),
    )
    count = store().one("SELECT COUNT(*) AS total FROM api_key_requests WHERE token_id=?", (token_id,))[
        "total"
    ]
    return {"requests": rows, "total": count}


@router.get("/api/tokens/{token_id}/usage")
def usage(token_id: str, request: Request):
    api_keys.owned_key(token_id, who(request).user_id)
    return {
        "days": store().query(
            "SELECT day,requests,failures FROM api_key_daily WHERE token_id=? ORDER BY day", (token_id,)
        ),
        "retention": {"detail_days": api_keys.DETAIL_DAYS, "aggregate_days": api_keys.AGGREGATE_DAYS},
    }


@router.post("/api/workflows/{name}/webhook-token", status_code=201)
async def webhook_token(name: str, request: Request, body: WebhookCreate = Body(default=WebhookCreate())):
    async with workflow_lock(name):
        return await _webhook_token(name, request, body)


async def _webhook_token(name: str, request: Request, body: WebhookCreate):
    principal = who(request)
    access.require_resource("workflow", name, principal)
    await workflow_definition(name)  # Do not mint credentials for a nonexistent workflow.
    if not settings.public_url:
        raise HTTPException(503, "Configure the public instance URL before creating webhook links")
    throttle("api-key-create:" + principal.user_id, 30, window=3600)
    identity = api_keys.workflow_identity(name)
    result = api_keys.issue(
        principal,
        f"Webhook · {name}",
        [{"scope": "workflows:trigger", "resources": [identity]}],
        None if body.days is None else time.time() + body.days * 86400,
        kind="webhook",
    )
    result["url"] = (
        f"{settings.public_url.rstrip('/')}/api/triggers/{quote(name, safe='')}"
        f"?token={quote(result['secret'], safe='')}"
    )
    result["expires_at"] = result["token"]["expires_at"]
    return result
