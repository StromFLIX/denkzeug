"""Private event projection and revocation-aware long-lived transports."""

from __future__ import annotations

import asyncio
import contextlib
import time

from . import access
from .db import db


def live(who) -> bool:
    if who is None or who.service:
        return False
    if getattr(who, "token_id", None):
        return bool(
            db.one(
                "SELECT 1 FROM api_keys k JOIN auth_users u ON u.id=k.owner_user_id WHERE k.id=? "
                "AND k.owner_user_id=? AND k.revoked_at IS NULL "
                "AND (k.expires_at IS NULL OR k.expires_at>?) AND u.status='active' "
                "AND u.role=? AND u.must_change_password=0",
                (who.token_id, who.user_id, time.time(), who.role),
            )
        )
    if not who.session_id:
        return bool(db.one("SELECT 1 FROM auth_users WHERE id=? AND status='active'", (who.user_id,)))
    now = time.time()
    return bool(
        db.one(
            "SELECT 1 FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE s.id=? AND s.user_id=? "
            "AND u.status='active' AND s.revoked_at IS NULL AND s.expires_at>? AND s.access_expires_at>? "
            "AND s.last_seen_at>? AND u.role=? AND u.must_change_password=0",
            (who.session_id, who.user_id, now, now, now - 86400, who.role),
        )
    )


def visible(event: dict, who) -> bool:
    if who is None or who.service:
        return False
    if event.get("chat_id"):
        row = db.one("SELECT owner_user_id FROM chats WHERE id=?", (event["chat_id"],))
        return bool(row and row["owner_user_id"] == who.user_id)
    if event.get("workflow_id"):
        return bool(
            db.one(
                "SELECT 1 FROM runs WHERE workflow_id=? AND owner_user_id=?",
                (event["workflow_id"], who.user_id),
            )
        )
    if event.get("user_id"):
        return event["user_id"] == who.user_id
    if event.get("app_id"):
        return access.allowed("app", event["app_id"], who)
    if event.get("workflow"):
        return access.allowed("workflow", event["workflow"], who)
    kind = event.get("kind", "")
    if kind == "connected":
        return True
    # Resource-less operational events go only to admins. Unknown/private event
    # families are denied even to admins rather than risking private payloads.
    return who.role == "admin" and kind.startswith(
        (
            "system.",
            "worker.",
            "settings.",
            "model.",
            "mcp.",
            "project.",
            "agent.profile.",
            "workflow.",
            "pi.package.",
            "creative.",
        )
    )


async def guarded(source, who):
    """Check every frame and at least every five seconds, even while source waits."""
    iterator = source.__aiter__()
    pending = None
    try:
        while live(who):
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait({pending}, timeout=5)
            if not done:
                continue
            try:
                value = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            if not live(who):
                return
            yield value
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        if hasattr(iterator, "aclose"):
            await iterator.aclose()
