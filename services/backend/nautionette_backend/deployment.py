"""One validated deployment path for the app, promotion, and agent tools."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel

from . import access, api_keys
from .clients import authoring
from .events import bus
from .runs import restart_worker
from .workflow_control import workflow_lock


class WorkflowSource(BaseModel):
    code: str
    message: str = ""


async def deploy(name: str, code: str, message: str = "") -> dict[str, Any]:
    async with workflow_lock(name):
        return await _deploy(name, code, message)


async def _deploy(name: str, code: str, message: str) -> dict[str, Any]:
    access.require_execution()
    access.register_resource("workflow", name)
    try:
        result = await authoring.deploy(name, code)
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", "Workflow deployment failed")
        except ValueError:
            detail = "Workflow deployment failed"
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    api_keys.workflow_identity(name)
    bus.publish("workflow.published", {"workflow": name, "message": message})
    result["worker_restart"] = await restart_worker()
    result["ready"] = bool(result["worker_restart"].get("ok"))
    return result
