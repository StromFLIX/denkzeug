"""Read-only deployment readiness on the private service protocol.

These are not human API endpoints or agent tools. INTERNAL_TOKEN is mandatory;
user cookies/device credentials do not authorize deployment operations.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import output_storage
from ..security import require_internal
from . import system

router = APIRouter(dependencies=[Depends(require_internal)], include_in_schema=False)


@router.get("/internal/system")
async def system_status() -> dict[str, Any]:
    return await system.system_status()


@router.get("/internal/settings/storage")
def storage_status() -> dict[str, Any]:
    state = output_storage.status()
    # Do not expose bucket/endpoint names, account/resource names, policy,
    # maintenance errors, provider diagnostics, or private filesystem paths.
    health = state.get("health", {}).get("status")
    filesystems = []
    for item in state.get("filesystems", []):
        if item.get("label") not in {"MaxIO backing filesystem", "Backend data filesystem"}:
            continue
        filesystems.append(
            {
                "label": item["label"],
                "available": item.get("available") is True,
                **{
                    key: item[key]
                    for key in ("total_bytes", "used_bytes", "available_bytes")
                    if type(item.get(key)) is int and item[key] >= 0
                },
            }
        )
    return {
        "health": {"status": "ok" if health == "ok" else "unavailable"},
        "filesystems": filesystems,
    }
