"""Workflows, the drafts waiting for approval, and their schedules."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from temporalio.service import RPCError, RPCStatusCode

from .. import access, local_apps
from ..clients import authoring, temporal
from ..db import db
from ..deployment import WorkflowSource, deploy
from ..events import bus
from ..runs import restart_worker
from ..schedules import ScheduleRequest, schedule_workflow_name, temporal_spec
from ..security import require_user
from ..workflow_control import workflow_lock
from ..workflow_graph import definition_graph
from ..workflow_inputs import input_options, validate_inputs

router = APIRouter(dependencies=[Depends(require_user)])

CHAT_MODES = {"same", "new"}


def _private_settings(name: str) -> dict:
    fields = dict(db.workflow_settings(name))
    if fields.get("chat_id"):
        row = db.one("SELECT owner_user_id FROM chats WHERE id=?", (fields["chat_id"],))
        if row is None or row["owner_user_id"] != access.human_id():
            fields["chat_id"] = None
    return fields


async def _schedules() -> list[dict[str, Any]]:
    try:
        return await temporal.schedules()
    except Exception:  # noqa: BLE001 - schedules are extra, not essential
        return []


def _legacy_schedule(name: str, schedules: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in schedules if item["id"] == f"schedule-{name}"), None)


@router.get("/api/workflows")
async def list_workflows() -> dict[str, Any]:
    workflows = access.visible("workflow", await authoring.list_workflows(), "name")
    by_workflow = defaultdict(list)
    who = access.principal()
    if who is not None and who.role == "admin":
        for item in await _schedules():
            by_workflow[item["workflow"]].append(item)
    for workflow in workflows:
        name = workflow["name"]
        workflow["schedules"] = by_workflow[name]
        workflow["schedule"] = _legacy_schedule(name, workflow["schedules"])
        workflow["settings"] = _private_settings(name)
    return {"workflows": workflows}


@router.get("/api/workflows/{name}")
async def get_workflow(name: str) -> dict[str, Any]:
    workflow = await authoring.get_workflow(name)
    workflow["graph"] = definition_graph(workflow.get("code", ""), name)
    owner = access.human_id()
    workflow["runs"] = db.list_runs(name, limit=25, owner_user_id=owner) if owner is not None else []
    if owner and workflow["runs"]:
        latest = workflow["runs"][0]
        latest.update(local_apps.workflow_view(latest["workflow_id"], owner))
    workflow["settings"] = _private_settings(name)
    who = access.principal()
    workflow["schedules"] = []
    workflow["schedule"] = None
    workflow["schedule_status"] = "hidden"
    if who is not None and who.role == "admin":
        try:
            schedules = []
            for schedule_id in await temporal.workflow_schedule_ids(name):
                try:
                    schedules.append(await temporal.schedule(name, schedule_id=schedule_id))
                except RPCError as exc:
                    if exc.status != RPCStatusCode.NOT_FOUND:
                        raise
            workflow["schedules"] = schedules
            workflow["schedule"] = _legacy_schedule(name, schedules)
            workflow["schedule_status"] = "available"
        except Exception:  # noqa: BLE001 - leave the workflow usable if Temporal is down
            workflow["schedule_status"] = "unavailable"
    return workflow


@router.get("/api/workflows/{name}/input-options")
async def workflow_input_options(
    name: str,
    field: str = Query(max_length=256),
    q: str = Query(default="", max_length=120),
    selected: str = Query(default="", max_length=128),
    offset: int = Query(default=0, ge=0, le=10000),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    access.require_resource("workflow", name)
    workflow = await authoring.get_workflow(name)
    return input_options(
        (workflow.get("manifest") or {}).get("inputs", {}), field, q, selected, offset, limit
    )


@router.patch("/api/workflows/{name}/settings")
async def patch_workflow_settings(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "disabled" in payload:
        fields["disabled"] = bool(payload["disabled"])
    if "chat_mode" in payload:
        if payload["chat_mode"] not in CHAT_MODES:
            raise HTTPException(status_code=400, detail="chat_mode must be 'same' or 'new'")
        fields["chat_mode"] = payload["chat_mode"]
    async with workflow_lock(name):
        # Save intent first. Manual/webhook starts fail closed immediately; the
        # periodic reconciler repairs a lost Temporal reply or backend restart.
        db.set_workflow_settings(name, fields)
        updated = _private_settings(name)
        bus.publish("workflow.settings", {"workflow": name, **fields})
        if "disabled" in fields:
            try:
                await temporal.set_workflow_disabled(name, updated["disabled"])
            except Exception as exc:
                raise HTTPException(
                    503,
                    detail={
                        "message": "Settings saved, but schedule synchronization is pending. "
                        "Temporal may still start scheduled work until synchronization succeeds. "
                        "Retry when Temporal is ready; background recovery will also retry.",
                        "settings": updated,
                        "schedule_sync_pending": True,
                    },
                ) from exc
        return updated


@router.post("/api/workflows/{name}/deploy")
async def deploy_workflow(name: str, payload: WorkflowSource) -> dict[str, Any]:
    """Validate and deploy a complete Python workflow, then reload workers. No approval required."""
    return await deploy(name, payload.code, payload.message)


@router.delete("/api/workflows/{name}")
async def delete_workflow(name: str) -> dict[str, Any]:
    async with workflow_lock(name):
        return await _delete_workflow(name)


async def _delete_workflow(name: str) -> dict[str, Any]:
    # Stop Temporal before removing the definition. If Temporal is unavailable,
    # leave the file/settings intact so deletion can be retried safely.
    try:
        for schedule_id in await temporal.workflow_schedule_ids(name):
            await temporal.delete_schedule(name, schedule_id=schedule_id)
    except Exception as exc:  # noqa: BLE001 - do not strand a live schedule by deleting its definition
        raise HTTPException(
            status_code=503,
            detail="Schedule cleanup failed; workflow was not deleted. Retry when Temporal is ready.",
        ) from exc
    result = await authoring.delete_workflow(name)
    db.forget_workflow(name)
    bus.publish("workflow.deleted", {"workflow": name})
    # The authoring service already requested a worker restart and reports it
    # in this result. A second background restart races the worker warming up.
    return result


@router.post("/api/workflows/validate")
async def validate_workflow(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return await authoring.validate(payload.get("name", ""), payload.get("code", ""))


@router.post("/api/workflows/{name}/schedule")
async def schedule_workflow(name: str, payload: ScheduleRequest) -> dict[str, Any]:
    """Set the legacy default schedule. Use the schedules collection to add more."""
    return await _save_schedule(name, payload)


@router.post("/api/workflows/{name}/schedules")
async def create_workflow_schedule(name: str, payload: ScheduleRequest) -> dict[str, Any]:
    """Add an independent schedule to a workflow."""
    result = await _save_schedule(name, payload, schedule_id=f"schedule-{name}--{uuid4().hex}")
    return {**result, "id": result["schedule_id"]}


def _check_schedule_id(name: str, schedule_id: str) -> None:
    if schedule_workflow_name(schedule_id) != name:
        raise HTTPException(404, "Schedule not found")


@router.put("/api/workflows/{name}/schedules/{schedule_id}")
async def update_workflow_schedule(name: str, schedule_id: str, payload: ScheduleRequest) -> dict[str, Any]:
    """Change one schedule's recurrence and saved inputs."""
    _check_schedule_id(name, schedule_id)
    result = await _save_schedule(name, payload, schedule_id=schedule_id, update_only=True)
    return {**result, "id": result["schedule_id"]}


async def _save_schedule(
    name: str, payload: ScheduleRequest, *, schedule_id: str | None = None, update_only: bool = False
) -> dict[str, Any]:
    async with workflow_lock(name):
        workflow = await authoring.get_workflow(name)
        manifest = workflow.get("manifest") or {}
        validate_inputs(name, manifest.get("inputs"), payload.input, scheduled=True)
        if db.workflow_settings(name)["disabled"]:
            raise HTTPException(409, f"{name} is disabled; enable it before changing its schedules")
        try:
            result = await temporal.set_schedule(
                name,
                temporal_spec(payload),
                payload.input,
                timeout_minutes=manifest.get("timeout_minutes", 30),
                **({"schedule_id": schedule_id, "update_only": update_only} if schedule_id else {}),
            )
        except RPCError as exc:
            if update_only and exc.status == RPCStatusCode.NOT_FOUND:
                raise HTTPException(404, "Schedule not found") from exc
            raise
    bus.publish(
        "workflow.scheduled",
        {"workflow": name, "frequency": payload.frequency, "timezone": payload.timezone},
    )
    return result


@router.delete("/api/workflows/{name}/schedule")
async def unschedule_workflow(name: str) -> dict[str, Any]:
    """Remove the legacy default schedule only."""
    return await _remove_schedule(name)


@router.delete("/api/workflows/{name}/schedules/{schedule_id}")
async def delete_workflow_schedule(name: str, schedule_id: str) -> dict[str, Any]:
    _check_schedule_id(name, schedule_id)
    return await _remove_schedule(name, schedule_id=schedule_id)


async def _remove_schedule(name: str, *, schedule_id: str | None = None) -> dict[str, Any]:
    async with workflow_lock(name):
        await temporal.delete_schedule(name, schedule_id=schedule_id)
    bus.publish("workflow.unscheduled", {"workflow": name})
    return {"ok": True}


# --------------------------------------------------------------------- drafts


@router.get("/api/drafts")
async def list_drafts() -> dict[str, Any]:
    return {"drafts": await authoring.list_drafts()}


@router.get("/api/drafts/{name}")
async def get_draft(name: str) -> dict[str, Any]:
    draft = await authoring.get_draft(name)
    draft["graph"] = definition_graph(draft.get("code", ""), name)
    draft["previous_graph"] = None
    if not draft.get("is_new", False):
        published = next((item for item in await authoring.list_workflows() if item["name"] == name), None)
        if published:
            previous = await authoring.get_workflow(name)
            draft["previous_graph"] = definition_graph(previous.get("code", ""), name)
    return draft


@router.post("/api/drafts/{name}/approve")
async def approve_draft(name: str) -> dict[str, Any]:
    published = await authoring.publish(name)
    bus.publish("workflow.published", {"workflow": name})
    published["worker_restart"] = await restart_worker()
    return published


@router.delete("/api/drafts/{name}")
async def discard_draft(name: str) -> dict[str, Any]:
    result = await authoring.discard(name)
    bus.publish("workflow.draft_discarded", {"workflow": name})
    return result
