"""Runs, and the triggers that start them."""

from __future__ import annotations

import base64
import json
import math
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from .. import access, run_statistics, runs
from ..clients import temporal
from ..db import db
from ..events import bus
from ..execution_graph import execution_graph
from ..security import require_user
from .internal import internal_run

router = APIRouter()


@router.post("/api/workflows/{name}/run", dependencies=[Depends(require_user)])
async def run_workflow(
    name: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    result = await runs.start(name, payload.get("input") or {}, trigger="manual")
    request.state.api_run_id = result.get("workflow_id")
    return result


@router.get("/api/runs", dependencies=[Depends(require_user)])
async def list_runs(
    workflow: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str, Query(max_length=256)] = "",
    status: Annotated[str, Query(max_length=32)] = "",
    trigger: Annotated[str, Query(max_length=32)] = "",
    q: Annotated[str, Query(max_length=120)] = "",
    compact: bool = False,
) -> dict[str, Any]:
    owner = access.human_id()
    if owner is None:
        raise HTTPException(403, "User account required")
    before = None
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if (
                not isinstance(value, list)
                or len(value) != 2
                or not isinstance(value[0], (int, float))
                or not math.isfinite(value[0])
                or not isinstance(value[1], str)
                or len(value[1]) > 128
            ):
                raise ValueError
            before = (value[0], value[1])
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "Invalid run cursor") from exc
    local = db.list_runs(
        workflow, limit + 1, owner_user_id=owner, before=before, status=status, trigger=trigger, search=q
    )
    more = len(local) > limit
    local = local[:limit]
    next_cursor = ""
    if more and local:
        next_cursor = (
            base64.urlsafe_b64encode(json.dumps([local[-1]["created_at"], local[-1]["id"]]).encode())
            .decode()
            .rstrip("=")
        )
    if compact:
        # Persisted states are also used for filters. Do not overlay a different
        # status from Temporal after filtering, or fetch histories per table row.
        columns = {"workflow_id", "workflow", "trigger", "status", "created_at", "updated_at"}
        return {
            "runs": [{key: value for key, value in row.items() if key in columns} for row in local],
            "next_cursor": next_cursor,
            "temporal": [],
        }
    try:
        remote = await temporal.recent(50)
    except Exception:  # noqa: BLE001
        remote = []
    owned = {row["workflow_id"] for row in local}
    remote = [item for item in remote if item["workflow_id"] in owned]
    by_id = {item["workflow_id"]: item for item in remote}
    for run in local:
        live = by_id.get(run["workflow_id"])
        if live:
            run["status"] = live["status"].lower()
            run["start_time"] = live["start_time"]
            run["close_time"] = live["close_time"]
    return {"runs": local, "temporal": remote, "next_cursor": next_cursor}


@router.get("/api/runs/{workflow_id}", dependencies=[Depends(require_user)])
async def get_run(workflow_id: str) -> dict[str, Any]:
    row, info = await runs.describe_run(workflow_id)
    if info is None:
        return {
            "run": row,
            "temporal": None,
            "history_status": "expired",
            "note": runs.HISTORY_EXPIRED_NOTE,
            "statistics": run_statistics.statistics(workflow_id, row),
        }
    if info["status"] == "COMPLETED" and (not row or row.get("result") is None):
        try:
            info["result"] = await temporal.result(workflow_id, timeout=10)
        except Exception:  # noqa: BLE001
            info["result"] = None
    delivery = db.one(
        "SELECT d.state,d.chat_id FROM run_deliveries d JOIN runs r ON r.id=d.run_id WHERE r.workflow_id=?",
        (workflow_id,),
    )
    if delivery and delivery["chat_id"]:
        chat = db.one("SELECT owner_user_id FROM chats WHERE id=?", (delivery["chat_id"],))
        if not chat or chat["owner_user_id"] != access.human_id():
            delivery["chat_id"] = None
    return {
        "run": row,
        "temporal": info,
        "delivery": delivery,
        "statistics": run_statistics.statistics(workflow_id, row),
    }


@router.get("/api/runs/{workflow_id}/graph", dependencies=[Depends(require_user)])
async def get_run_graph(workflow_id: str) -> dict[str, Any]:
    row, info = await runs.describe_run(workflow_id)
    history, history_status = await runs.retained_history(workflow_id, info, limit=2001)
    if info is None:
        saved = runs.digest(row)
        info = {
            "workflow_id": workflow_id,
            "run_id": row.get("run_id"),
            "workflow_type": row["workflow"],
            "status": saved["status"],
            "start_time": saved["started"],
            "close_time": None,
        }
    graph = execution_graph(info, history[:2000], truncated=len(history) > 2000)
    graph["history_status"] = history_status
    if history_status == "expired":
        graph["warnings"] = [runs.HISTORY_EXPIRED_NOTE]
        if row:
            graph["nodes"][0].update(input=row["input"], result=row["result"])
    return graph


@router.get("/api/runs/{workflow_id}/history", dependencies=[Depends(require_user)])
async def read_run(workflow_id: str, limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    """Inspect a run's inputs, activity results, failures, and final result before repairing it."""
    return await internal_run(workflow_id, limit)


@router.post("/api/runs/{workflow_id}/cancel", dependencies=[Depends(require_user)])
async def cancel_run(workflow_id: str) -> dict[str, Any]:
    await temporal.cancel(workflow_id)
    return {"ok": True}


@router.post("/api/runs/{workflow_id}/terminate", dependencies=[Depends(require_user)])
async def terminate_run(workflow_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """For a run that cannot be asked nicely, because its worker cannot load it."""
    await temporal.terminate(workflow_id, payload.get("reason") or "terminated from the app")
    row = runs.stored_run(workflow_id)
    if row:
        await runs.complete_run(
            row["workflow"],
            workflow_id,
            "terminated",
            {"summary": payload.get("reason") or "Terminated from the app."},
        )
    bus.publish("run.terminated", {"workflow_id": workflow_id})
    return {"ok": True}


async def _get_trigger_input(name: str, request: Request) -> dict[str, Any]:
    if len(request.scope.get("query_string", b"")) > 16384:
        raise HTTPException(413, "Workflow trigger query is too large")
    parameters = request.query_params
    if any(len(parameters.getlist(key)) != 1 for key in parameters):
        raise HTTPException(422, "Workflow trigger parameters must not be repeated")
    if "input" in parameters:
        if set(parameters) - {"token", "input"}:
            raise HTTPException(422, "Use either JSON input or individual query fields, not both")
        try:
            payload = json.loads(parameters["input"])
        except ValueError as error:
            raise HTTPException(422, "Workflow trigger input must be a JSON object") from error
        if not isinstance(payload, dict):
            raise HTTPException(422, "Workflow trigger input must be a JSON object")
        return payload
    # Compatibility with integrations that can only send individual URL fields.
    # Preserve string values; decode JSON for explicitly non-string schema types.
    payload = {key: value for key, value in parameters.items() if key != "token"}
    if payload:
        from ..clients import authoring

        definition = await authoring.get_workflow(name)
        properties = ((definition.get("manifest") or {}).get("inputs") or {}).get("properties", {})
        for key, value in list(payload.items()):
            expected = properties.get(key, {}).get("type")
            types = expected if isinstance(expected, list) else [expected]
            if "string" in types or not any(types):
                continue
            try:
                payload[key] = json.loads(value)
            except ValueError:
                pass  # The existing manifest validator returns the field error.
    return payload


@router.get("/api/triggers/{name}", dependencies=[Depends(require_user)])
@router.post("/api/triggers/{name}", dependencies=[Depends(require_user)])
async def trigger(name: str, request: Request, token: str | None = Query(default=None)) -> dict[str, Any]:
    """Scoped integration triggers. GET requires an explicit API/workflow token, never just cookies."""
    access.require_execution()
    if request.method == "GET":
        payload = await _get_trigger_input(name, request)
        result = await runs.start(name, payload, trigger="webhook")
        request.state.api_run_id = result.get("workflow_id")
        return result
    try:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1048576:
                raise HTTPException(413, "Workflow webhook payload is too large")
            body.extend(chunk)
        payload = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, "Workflow webhook body must be valid JSON") from exc
    if not isinstance(payload, dict):
        payload = {"payload": payload}
    result = await runs.start(name, payload, trigger="webhook")
    request.state.api_run_id = result.get("workflow_id")
    return result
