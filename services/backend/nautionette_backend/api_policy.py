"""Reviewed, exhaustive API policies. New operations MUST be added to the manifest.

Never infer authorization from a path prefix, router, HTTP method or admin role.
The explicit manifest is validated at assembly/startup and used at admission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.routing import iter_route_contexts
from starlette.routing import Match, Mount, WebSocketRoute

# Scope descriptions are also the public key-creation catalogue.
DOMAINS = {
    "workflows": ("read", "write", "delete", "trigger", "schedule"),
    "runs": ("read", "cancel", "terminate"),
    "mcp": ("read", "execute", "manage"),
    "integrations": ("read", "manage"),
    "chats": ("read", "write", "delete"),
    "agents": ("read", "write"),
    "projects": ("read", "write"),
    "miniapps": ("read", "write", "invoke"),
    "outputs": ("read", "write"),
    "settings": ("read", "write"),
    "tokens": ("read", "manage"),
    "system": ("read", "control"),
    "audit": ("read",),
    "accounts": ("read", "manage"),
    "access": ("read", "manage"),
    "packages": ("read", "manage"),
    "artcraft": ("read", "manage"),
    "speech": ("read", "execute"),
}
RESOURCE_KINDS = {
    "workflows": "workflow",
    "runs": "run",
    "chats": "chat",
    "agents": "agent",
    "projects": "project",
    "miniapps": "app",
    "outputs": "output",
    "mcp": "tool",
}
SCOPES = {
    f"{domain}:{action}": {
        "scope": f"{domain}:{action}",
        "description": f"{action.capitalize()} {domain}",
        "resource_kind": RESOURCE_KINDS.get(domain),
    }
    for domain, actions in DOMAINS.items()
    for action in actions
}


@dataclass(frozen=True)
class Policy:
    scope: str | None
    authentication: str = "account"
    resource: str | None = None


MANIFEST = json.loads((Path(__file__).with_name("data") / "api-policies.json").read_text())
POLICIES = {key: Policy(**value) for key, value in MANIFEST.items()}
# Connection configuration is a collection surface, not a tool-execution grant.
SCOPES["chats:write"]["description"] = (
    "Change chat metadata, drafts and attachments; agent turns require a user session"
)
SCOPES["workflows:write"]["description"] = "Deploy executable workflow code and change its configuration"
SCOPES["workflows:trigger"]["description"] = "Start existing workflows; no code, schedule or history access"
SCOPES["runs:read"]["description"] = "Read your run inputs, results and activity history"
SCOPES["runs:terminate"]["description"] = (
    "Force-terminate your runs when graceful cancellation is unavailable"
)
SCOPES["miniapps:write"]["description"] = "Publish executable app code, manage releases and delete apps"
SCOPES["mcp:manage"]["description"] = "Configure MCP connections and their credentials (administrator)"
SCOPES["system:control"]["description"] = "Restart workers and perform operational maintenance"
SCOPES["tokens:read"]["description"] = "Inspect your key metadata and request history; never reveal secrets"
SCOPES["mcp:read"]["resource_kind"] = None
SCOPES["mcp:manage"]["resource_kind"] = None
SCOPES["mcp:execute"]["description"] = (
    "Execute selected backend MCP tools (also requires each tool's API scope)"
)
AUTHENTICATION = {
    "account",
    "session",
    "delegated",
    "internal",
    "capability",
    "protocol",
    "public",
    "mcp",
    "ticket",
}


def validate_routes(app, prefix: str = "") -> None:
    """Fail startup/CI for missing policies, including hidden routes and mounts."""
    for route in iter_route_contexts(app.routes):
        if isinstance(route.original_route, Mount):
            keys = [f"MOUNT {prefix}{route.path}"]
        elif isinstance(route.original_route, WebSocketRoute):
            keys = [f"WEBSOCKET {prefix}{route.path}"]
        else:
            keys = [f"{method} {prefix}{route.path}" for method in route.methods or []]
        for key in keys:
            policy = POLICIES.get(key)
            if policy is None:
                raise RuntimeError(f"API operation has no explicit authentication/scope policy: {key}")
            if policy.scope is not None and policy.scope not in SCOPES:
                raise RuntimeError(f"Unknown API scope: {key}: {policy.scope}")
            if policy.authentication not in AUTHENTICATION:
                raise RuntimeError(f"Unknown authentication policy: {key}")
            if policy.scope is None and policy.authentication in {"account", "internal", "capability", "mcp"}:
                raise RuntimeError(f"Authenticated API operation must declare its scope: {key}")
        if isinstance(route.original_route, Mount):
            mounted = route.original_route.app
            if not hasattr(mounted, "routes"):
                raise RuntimeError(f"Mounted API must expose routes for policy review: {route.path}")
            validate_routes(mounted, prefix + route.path)
        if hasattr(route, "openapi_extra") and keys:
            route.original_route.openapi_extra = {
                **(route.openapi_extra or {}),
                "x-denkzeug-policy": MANIFEST[keys[0]],
            }


def annotate_openapi(app) -> None:
    # MCP discovery may have built/cached OpenAPI before app assembly finished.
    # Annotate the final document, not only lazily included route contexts.
    original = app.openapi

    def openapi():
        document = original()
        for path, operations in document.get("paths", {}).items():
            for method, operation in operations.items():
                policy = MANIFEST.get(f"{method.upper()} {path}")
                if policy is not None:
                    operation["x-denkzeug-policy"] = policy
        return document

    app.openapi = openapi


def resolve(request: Request, app):
    for route in iter_route_contexts(app.routes):
        match, child = route.matches(request.scope)
        if match != Match.FULL:
            continue
        key = (
            f"MOUNT {route.path}"
            if isinstance(route.original_route, Mount)
            else f"{request.method} {route.path}"
        )
        policy = POLICIES.get(key)
        if policy is None:
            raise HTTPException(403, "This operation has no security policy")
        # The frontend is intentionally public, but may never act as an API fallback.
        if route.path == "/{path:path}" and request.url.path.startswith(("/api", "/internal", "/mcp")):
            raise HTTPException(404, "not found")
        request.state.api_policy = policy
        request.state.api_route = route.path
        request.state.api_params = child.get("path_params", {})
        return policy
    raise HTTPException(404, "not found")


async def admit(request: Request, app) -> None:
    policy = resolve(request, app)
    if policy.authentication in {"public", "protocol", "ticket", "mcp"}:
        return  # Explicit, purpose-specific protocol verifier; never ambient account access.
    from .security import require_internal, require_user

    if policy.authentication == "internal":
        await require_internal(request, request.headers.get("x-internal-token"))
    else:
        who = await require_user(request)
        if policy.authentication == "session" and (who.service or who.token_id or not who.session_id):
            raise HTTPException(403, "This operation requires a signed-in user")
        if policy.authentication == "delegated" and (who.token_id or who.service):
            raise HTTPException(403, "This operation requires a user or live delegated account")
        if policy.authentication == "capability" and who.token_id:
            raise HTTPException(403, "This operation requires a live delegated capability")
