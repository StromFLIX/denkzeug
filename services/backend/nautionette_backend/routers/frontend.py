"""Artifacts, and the single door in front of the SPA.

This router is included last: its catch-all would otherwise swallow every other
route in the app.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..clients.http import shared
from ..config import settings
from ..security import require_user

router = APIRouter()

# Hop-by-hop and length headers describe the upstream response, not ours.
_EXCLUDED_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding"}


@router.get("/api/artifacts/{name}", dependencies=[Depends(require_user)])
async def get_artifact(name: str) -> Response:
    path = Path(settings.artifacts_dir) / os.path.basename(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


def app_sandbox_url() -> str:
    from ..app_sandbox import origin

    configured = os.environ.get("MCP_APPS_SANDBOX_URL", "").strip()
    if not configured:
        return ""
    try:
        sandbox = origin(configured)
        if settings.public_url and sandbox == origin(settings.public_url):
            return ""
        return sandbox
    except ValueError:
        return ""


@router.get("/api/mcp-apps/runtime", dependencies=[Depends(require_user)])
async def mcp_apps_runtime() -> dict:
    """Browser sandbox configuration. No credentials or application authority."""
    return {"sandbox_url": app_sandbox_url(), "capabilities": {"openLinks": {}}}


@router.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def frontend(path: str, request: Request) -> Response:
    """One door: the SPA is served through the backend, not published itself."""
    if path.startswith(("api/", "internal/")):
        return JSONResponse({"detail": "not found"}, status_code=404)
    target = f"{settings.frontend_web_url.rstrip('/')}/{path}"
    try:
        upstream = await shared().request(
            request.method, target, params=dict(request.query_params), timeout=15
        )
    except httpx.HTTPError as exc:
        return PlainTextResponse(f"frontend unavailable: {exc}", status_code=502)
    headers = {key: value for key, value in upstream.headers.items() if key.lower() not in _EXCLUDED_HEADERS}
    content = upstream.content
    if "text/html" in upstream.headers.get("content-type", ""):
        # Packaged clients permit configured HTTPS sandbox origins; the web
        # shell tightens this to the one deployment-specific origin.
        content = content.replace(
            b"frame-src 'self' https: http://localhost:8092 http://127.0.0.1:8092",
            ("frame-src 'self'" + (" " + app_sandbox_url() if app_sandbox_url() else "")).encode(),
        )
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )
