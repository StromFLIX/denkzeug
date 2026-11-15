"""The backend: the only service that publishes a port.

Auth, chats, workflows, triggers and the stream back to the clients all live
here. It never touches the Docker socket; the broker does that.

This module only assembles the app; every route lives in `routers/`.
"""

from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from nautionette.brand import NAME
from nautionette.telemetry import instrument_app

from . import (
    access,
    api_keys,
    artcraft,
    auth,
    auth_mail,
    github_mentions,
    mcp_oauth,
    mcp_servers,
    miniapps,
    output_maintenance,
    pi_packages,
    runs,
    worktree_maintenance,
)
from .access_logging import install as install_access_logging
from .api_policy import annotate_openapi, validate_routes
from .backend_mcp import BackendMCP
from .background import drain, spawn
from .chat_search import search
from .clients.http import close_shared
from .config import settings
from .conversations import recover_chat_agents, recover_interrupted
from .events import bus
from .integrations import bootstrap
from .routers import ROUTERS
from .security import AccountBoundaryMiddleware

install_access_logging()


def seed_workflows() -> None:
    """Copy committed workflows into the shared volume on first start."""
    source, target = Path(settings.seed_dir), Path(settings.workflows_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / ".drafts").mkdir(exist_ok=True)
    if not source.exists():
        return
    for item in source.glob("*.py"):
        destination = target / item.name
        if not destination.exists():
            shutil.copy2(item, destination)
        # Environment bootstrap happens before seeding. Newly installed example
        # definitions still need an explicit workspace owner and can then be shared.
        from .db import db

        state = db.one("SELECT legacy_owner FROM access_state WHERE id=1")
        if state and state["legacy_owner"]:
            access.register_resource("workflow", item.stem, owner_user_id=state["legacy_owner"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.internal_token:
        raise ValueError("INTERNAL_TOKEN is required for authenticated internal services")
    validate_routes(app)
    auth.initialize()
    spawn(api_keys.maintenance(), name="api-key-audit-retention")
    spawn(auth_mail.maintenance(), name="account-mail-maintenance")
    spawn(access.account_maintenance(), name="account-access-maintenance")
    Path(settings.artifacts_dir).mkdir(parents=True, exist_ok=True)
    seed_workflows()
    recover_interrupted()
    pi_packages.recover_installations()
    artcraft.recover_installations()
    spawn(artcraft.maintenance(), name="creative-app-maintenance")
    spawn(recover_chat_agents(), name="chat-agent-recovery")
    spawn(github_mentions.consume(), name="github-mention-inbox")
    spawn(output_maintenance.run(), name="output-storage-maintenance")
    spawn(worktree_maintenance.run(), name="worktree-retention")
    spawn(mcp_oauth.maintenance(), name="mcp-oauth-maintenance")
    spawn(miniapps.maintenance(), name="miniapp-maintenance")
    spawn(search.run(), name="conversation-search-indexer")
    # agentgateway may still be starting, so this retries in the background
    # rather than holding the port closed.
    spawn(bootstrap(), name="integration-bootstrap")
    runs.resume_unfinished()
    spawn(runs.reconcile_schedules(), name="schedule-reconciliation")
    spawn(runs.reconcile_deliveries(), name="workflow-result-delivery")
    bus.publish("system.start", {"version": settings.version})
    async with backend_mcp.http_app.router.lifespan_context(backend_mcp.http_app):
        spawn(mcp_servers.bootstrap_backend(), name="backend-mcp-bootstrap")
        try:
            yield
        finally:
            await drain()
            await close_shared()


app = FastAPI(
    title=NAME,
    version=settings.version,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
    openapi_url="/api/openapi.json",
)

# Web uses same-origin HttpOnly sessions; packaged clients use bearer sessions.
# Credentialed cross-origin cookies are intentionally not enabled.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Denkzeug-Account"],
)

app.add_middleware(AccountBoundaryMiddleware)


@app.exception_handler(RequestValidationError)
async def account_validation_error(request: Request, exc: RequestValidationError):
    # Pydantic's default error includes submitted values, including passwords and
    # recovery tokens. Never reflect those into responses or telemetry.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
                for error in exc.errors()
            ]
        },
    )


for router in ROUTERS[:-1]:
    app.include_router(router)

backend_mcp = BackendMCP(app, ROUTERS[:-1])
app.mount("/mcp", backend_mcp.http_app)
app.include_router(ROUTERS[-1])
validate_routes(app)
annotate_openapi(app)
instrument_app(app, "nautionette-backend")
