"""Health, the status page and the event feed behind it."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response, StreamingResponse

from .. import runs
from ..account_streams import guarded, visible
from ..background import is_draining, spawn
from ..clients import authoring, broker, gateway, temporal
from ..config import settings
from ..events import bus
from ..runtime import agent_has_answered
from ..security import current_principal, require_user

router = APIRouter()

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
HEALTH_TTL = 30.0
PROBE_TIMEOUT = 15.0
_snapshot: tuple[float, dict[str, Any]] | None = None
_refresh_task: asyncio.Task[dict[str, Any]] | None = None


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": settings.version,
        "build_commit": settings.build_commit,
        "release_tag": settings.release_tag,
        "environment": settings.environment,
        "chat_draining": is_draining(),
    }


async def probe_components() -> dict[str, Any]:
    """Run live component checks; deployment readiness never reads the snapshot."""

    async def temporal_health() -> dict[str, Any]:
        ok = await temporal.healthy()
        return {
            "name": "temporal",
            "status": "ok" if ok else "down",
            "detail": temporal.last_error or settings.temporal_address,
        }

    async def safe(coro, name: str) -> dict[str, Any]:
        try:
            detail = await asyncio.wait_for(coro, PROBE_TIMEOUT)
            if name == "temporal":
                return detail
            status = "ok"
            if isinstance(detail, dict) and detail.get("status", "ok") != "ok":
                status = "degraded"
            return {"name": name, "status": status, "detail": detail}
        except TimeoutError:
            return {"name": name, "status": "down", "detail": "Health check timed out"}
        except Exception as exc:  # noqa: BLE001 - status page must never 500
            return {"name": name, "status": "down", "detail": str(exc)[:200]}

    temporal_state, broker_state, gateway_state, authoring_state = await asyncio.gather(
        safe(temporal_health(), "temporal"),
        safe(broker.health(), "broker"),
        safe(gateway.health(), "agentgateway"),
        safe(authoring.health(), "workflow-mcp"),
    )
    agent_sets: list[dict[str, Any]] = []
    if broker_state["status"] != "down":
        try:
            agent_sets = await asyncio.wait_for(broker.agent_sets(), 5)
        except Exception:  # noqa: BLE001
            agent_sets = []
    return {
        "components": [temporal_state, broker_state, gateway_state, authoring_state],
        "agent_sets": agent_sets,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def status_payload(snapshot: dict[str, Any], *, stale: bool = False) -> dict[str, Any]:
    return {
        "version": settings.version,
        "build_commit": settings.build_commit,
        "release_tag": settings.release_tag,
        "environment": settings.environment,
        "auth_enabled": settings.auth_enabled,
        "model": settings.agent_model,
        "model_key_present": settings.model_key_present or agent_has_answered(),
        **snapshot,
        "stale": stale,
    }


async def _refresh_snapshot() -> dict[str, Any]:
    global _snapshot
    value = await probe_components()
    _snapshot = (time.monotonic(), value)
    return value


def refresh_snapshot() -> asyncio.Task[dict[str, Any]]:
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = spawn(_refresh_snapshot(), name="system-health-snapshot")
    return _refresh_task


@router.get("/api/system", dependencies=[Depends(require_user)])
async def system_status(refresh: bool = False) -> dict[str, Any]:
    """Report component health. Set refresh to wait for live checks."""
    if refresh or _snapshot is None:
        return status_payload(await asyncio.shield(refresh_snapshot()))
    checked, value = _snapshot
    stale = time.monotonic() - checked >= HEALTH_TTL
    if stale:
        refresh_snapshot()
    return status_payload(value, stale=stale)


@router.head("/api/events", dependencies=[Depends(require_user)], include_in_schema=False)
async def probe_events() -> Response:
    """Let SSE clients distinguish auth failures without opening another stream."""
    return Response(status_code=204, headers=SSE_HEADERS)


@router.get("/api/events", dependencies=[Depends(require_user)])
async def events_stream() -> StreamingResponse:
    who = current_principal.get()
    return StreamingResponse(
        guarded(bus.stream(who), who), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.get("/api/events/recent", dependencies=[Depends(require_user)])
async def events_recent() -> dict[str, Any]:
    who = current_principal.get()
    return {"events": [event for event in bus.recent(100) if visible(event, who)]}


@router.post("/api/system/worker/restart", dependencies=[Depends(require_user)])
async def restart_workers() -> dict[str, Any]:
    """Reload the workflow workers through the Docker broker's fixed restart operation."""
    return await runs.restart_worker()
