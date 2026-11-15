"""OAuth browser return and public client metadata; never a general auth bypass.

The return accepts only a live state capability created by an authenticated MCP
administrator. It cannot choose an endpoint, publish a target or return tokens.
"""

from __future__ import annotations

import asyncio
import secrets
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from nautionette.brand import NAME

from .. import mcp_http_oauth, mcp_oauth
from .. import mcp_oauth_provider as provider

router = APIRouter()
CALLBACK_BODY_SECONDS = 10


def headers():
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


@router.get("/api/mcp-oauth/client-metadata")
async def client_metadata():
    return JSONResponse(
        {
            "client_id": provider.metadata_client_url(),
            "client_name": NAME,
            "redirect_uris": [provider.callback_url()],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        headers=headers(),
    )


@router.get("/api/mcp-oauth/web-callback")
@router.post("/api/mcp-oauth/web-callback")
async def browser_return(request: Request):
    try:
        if request.method == "POST":
            if (
                request.headers.get("content-type", "").split(";", 1)[0]
                != "application/x-www-form-urlencoded"
            ):
                raise HTTPException(400, "Invalid OAuth return format")
            body = bytearray()
            async with asyncio.timeout(CALLBACK_BODY_SECONDS):
                async for block in request.stream():
                    body.extend(block)
                    if len(body) > mcp_http_oauth.LIMIT:
                        raise HTTPException(400, "OAuth return is too large")
            raw = body.decode("utf-8")
        else:
            raw = request.url.query
        if len(raw.encode()) > mcp_http_oauth.LIMIT:
            raise HTTPException(400, "OAuth return is too large")
        parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=32)
        if any(len(values) != 1 for values in parsed.values()):
            raise HTTPException(400, "Duplicate OAuth return fields")
        async with mcp_oauth._lock:
            await mcp_http_oauth.web_callback({k: v[0] for k, v in parsed.items()}, request.method)
        status, message = 200, "Authorization received. Return to the app to finish checking the connection."
    except (HTTPException, ValueError, UnicodeError, TimeoutError):
        status, message = (
            400,
            "Authorization could not be completed. Return to the app and start a new login.",
        )
    nonce = secrets.token_urlsafe(24)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>OAuth return</title>"
        f"<script nonce='{nonce}'>history.replaceState(null,'',location.pathname)</script></head>"
        f"<body><p>{message}</p></body></html>",
        status_code=status,
        headers={
            **headers(),
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )
