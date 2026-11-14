"""MCP authoring uses account capabilities, not the service's ambient authority."""

from __future__ import annotations

import json

import httpx
from starlette.responses import JSONResponse

from . import backend


class ScopedWorkflowMCP:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope.get("path", "").rstrip("/") != "/mcp"
        ):
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 1_048_576:
                return await JSONResponse({"error": "MCP request too large"}, status_code=413)(
                    scope, receive, send
                )
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return await JSONResponse({"error": "A single JSON-RPC request is required"}, status_code=400)(
                scope, receive, send
            )
        if payload.get("method") == "tools/call":
            params = payload.get("params")
            args = params.get("arguments", {}) if isinstance(params, dict) else None
            request_id = payload.get("id")
            if (
                not isinstance(params, dict)
                or not isinstance(args, dict)
                or type(request_id) not in {str, int}
                or payload.get("jsonrpc") != "2.0"
            ):
                return await JSONResponse({"error": "Invalid MCP call"}, status_code=400)(
                    scope, receive, send
                )
            args = dict(args)
            grant = args.pop("__denkzeug_actor", None)
            result = {
                "content": [{"type": "text", "text": "A live account-scoped agent capability is required"}],
                "isError": True,
            }
            if isinstance(grant, str) and 0 < len(grant) <= 128:
                try:
                    result = await backend.post(
                        "/internal/mcp/workflow-call",
                        {
                            "tool": params.get("name"),
                            "arguments": args,
                            "grant": grant,
                        },
                    )
                except httpx.HTTPError:
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": "The account-scoped workflow request was rejected or unavailable",
                            }
                        ],
                        "isError": True,
                    }
            return await JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})(
                scope, receive, send
            )
        delivered, original_receive = False, receive

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original_receive()

        return await self.app(scope, replay, send)
