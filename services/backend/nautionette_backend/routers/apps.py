"""Publish private single-file MCP Apps; inspect pinned workflow instances."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints

from .. import local_apps
from ..security import require_user

router = APIRouter(dependencies=[Depends(require_user)])
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=80)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class AppPublish(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: Slug
    title: Title
    description: str = Field(default="", max_length=2000)
    html: str = Field(min_length=1, max_length=local_apps.MAX_HTML_BYTES)
    input_schema: dict[str, Any]
    sample_data: dict[str, Any]
    change_note: str = Field(default="", max_length=2000)
    expected_revision: StrictInt = Field(ge=0, le=local_apps.MAX_REVISIONS)


class AppArchive(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    archived: StrictBool
    expected_revision: StrictInt = Field(ge=1, le=local_apps.MAX_REVISIONS)


@router.get("/api/apps", name="list_apps")
async def list_apps():
    """List this account's private user-authored MCP Apps, including archived apps."""
    return local_apps.list_apps()


@router.post("/api/apps", name="publish_app")
async def publish_app(body: AppPublish):
    """Publish an immutable single-file HTML app revision (0 creates a new slug).

    Use a self-contained HTML document, a Draft 2020-12 JSON schema with only
    local fragment references, and matching sample_data. Apps run only in the
    client's isolated sandbox with network denied; there is no server build.
    Workflow results may use {summary, data, ui: {app: slug, revision?: number}}.
    """
    return local_apps.publish(body)


@router.get("/api/apps/{app_id}", name="get_app")
async def get_app(app_id: Slug):
    """Get a private app's summary, revision history, and active revision source."""
    return local_apps.get_app(app_id)


@router.get("/api/apps/{app_id}/revisions/{revision}", name="get_app_revision")
async def get_app_revision(app_id: Slug, revision: Annotated[int, Path(ge=1)]):
    """Read immutable HTML, schema, sample data and hash for a private app revision."""
    return local_apps.get_revision(app_id, revision)


@router.patch("/api/apps/{app_id}", name="archive_app")
async def archive_app(app_id: Slug, body: AppArchive):
    """Archive/unarchive an app; existing snapshots survive, new references are blocked."""
    return local_apps.archive(app_id, body.archived, body.expected_revision)


@router.get("/api/chats/{chat_id}/apps/{instance_id}", name="get_chat_app")
async def get_chat_app(chat_id: str, instance_id: str):
    """Read an immutable app/data snapshot belonging to your chat."""
    return local_apps.get_instance(chat_id, instance_id)
