"""Private adapters: service transport plus bounded, live account capabilities.

Neither endpoint exchanges INTERNAL_TOKEN for ambient administrator authority.
The workflow service/worker supplies a live turn capability or verified Temporal
activity provenance; all human resource operations still pass normal API guards.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .. import access, provenance
from ..auth_store import fail
from ..db import db
from ..security import Principal, require_internal
from .internal import WorkflowCaller

router = APIRouter(dependencies=[Depends(require_internal)], include_in_schema=False)


class WorkflowTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal[
        "list_workflows",
        "read_workflow",
        "validate_workflow",
        "write_workflow",
        "run_workflow",
        "delete_workflow",
        "list_runs",
        "read_run",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)
    grant: str = Field(min_length=1, max_length=128)


class ActivityGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: WorkflowCaller
    tool: str = Field(min_length=1, max_length=160)


@router.get("/internal/mcp/targets")
async def workflow_mcp_targets() -> dict:
    # The worker needs only names for routing. Never send it gateway admin
    # configuration, target URLs or the credentials in hybrid resources.
    from ..clients import gateway

    config = await gateway.config()
    targets = config["targets"]
    mode = config.get("mcp_prefix_mode", "conditional")
    if mode == "never" or (len(targets) < 2 and mode != "always"):
        return {"targets": []}  # Raw names carry no reliable ownership information.
    return {"targets": [target["name"] for target in targets]}


@router.post("/internal/auth/activity-grant")
async def activity_grant(body: ActivityGrant) -> dict:
    if not body.tool.startswith(("backend_", "workflows_")):
        fail(422, "invalid_tool", "This adapter is only for first-party account-scoped tools.")
    source = body.source.model_dump()
    await provenance.workflow_call(source)
    owner = db.one(
        "SELECT u.id,u.role,u.status FROM runs r JOIN auth_users u ON u.id=r.owner_user_id "
        "WHERE r.workflow_id=?",
        (source["workflow_id"],),
    )
    if not owner:
        # Scheduled run discovery is eventually consistent; provenance above was
        # verified against Temporal before resolving its durable resource owner.
        owner = db.one(
            "SELECT u.id,u.role,u.status FROM access_resources r JOIN auth_users u "
            "ON u.id=r.owner_user_id WHERE r.kind='workflow' AND r.resource_id=?",
            (source["workflow"],),
        )
    if not owner or owner["status"] != "active":
        fail(403, "owner_unavailable", "This workflow owner is not active.")
    who = Principal(owner["id"], owner["role"])
    access.require_resource("workflow", source["workflow"], who)
    if who.role != "admin" and not access.allowed("tool", body.tool, who):
        fail(403, "tool_not_shared", "This activity tool is not shared with the workflow caller.")
    token = access.issue_activity_grant(owner["id"], source["workflow_id"], [body.tool])
    if body.tool.startswith("backend_"):
        from ..main import backend_mcp

        operation = backend_mcp.operations.get(body.tool.removeprefix("backend_"), {})
        read_only = operation.get("method") in {"GET", "HEAD"}
    else:
        read_only = body.tool in {
            "workflows_list_workflows",
            "workflows_read_workflow",
            "workflows_list_runs",
            "workflows_read_run",
        }
    return {"grant": token, "read_only": read_only}


class ActivityRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant: str = Field(min_length=1, max_length=128)


@router.post("/internal/auth/revoke-activity-grant")
def revoke_activity_grant(body: ActivityRevoke) -> dict:
    access.release_activity_grant(body.grant)
    return {"revoked": True}


@router.post("/internal/mcp/workflow-call")
async def workflow_call(body: WorkflowTool) -> dict:
    from ..main import backend_mcp

    fields = {
        "list_workflows": set(),
        "read_workflow": {"name"},
        "validate_workflow": {"name", "code"},
        "write_workflow": {"name", "code", "message"},
        "run_workflow": {"name", "input"},
        "delete_workflow": {"name"},
        "list_runs": {"workflow", "limit"},
        "read_run": {"workflow_id", "limit"},
    }
    args = body.arguments
    if args.keys() - fields[body.tool]:
        return backend_mcp.result({"error": "Unknown workflow tool arguments"}, error=True).model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    if body.tool == "list_workflows":
        operation, mapped = "list_workflows", {}
    elif body.tool == "read_workflow":
        operation, mapped = "get_workflow", {"name": args.get("name")}
    elif body.tool == "validate_workflow":
        operation, mapped = (
            "validate_workflow",
            {"body": {"name": args.get("name"), "code": args.get("code")}},
        )
    elif body.tool == "write_workflow":
        operation, mapped = (
            "deploy_workflow",
            {
                "name": args.get("name"),
                "body": {"code": args.get("code"), "message": args.get("message", "")},
            },
        )
    elif body.tool == "run_workflow":
        operation, mapped = (
            "run_workflow",
            {"name": args.get("name"), "body": {"input": args.get("input") or {}}},
        )
    elif body.tool == "delete_workflow":
        operation, mapped = "delete_workflow", {"name": args.get("name")}
    elif body.tool == "list_runs":
        operation, mapped = "list_runs", {"workflow": args.get("workflow") or None}
    else:
        operation, mapped = (
            "read_run",
            {"workflow_id": args.get("workflow_id"), "limit": args.get("limit", 200)},
        )
    result = await backend_mcp.call(
        operation, {**mapped, "__denkzeug_actor": body.grant}, selection_tool=f"workflows_{body.tool}"
    )
    if not result.is_error and body.tool == "list_runs":
        limit = args.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 200:
            result = backend_mcp.result({"error": "Use a run limit between 1 and 200"}, error=True)
        else:
            value = json.loads(result.content[0].text)
            value["runs"] = value.get("runs", [])[:limit]
            value["temporal"] = value.get("temporal", [])[:limit]
            result = backend_mcp.result(value)
    if not result.is_error and body.tool == "list_workflows":
        value = json.loads(result.content[0].text)
        value["drafts"] = []
        result = backend_mcp.result(value)
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)
