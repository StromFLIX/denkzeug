"""Running one agent call.

Every agent call is one container run. Nothing is remembered inside the
container, so the history the model sees is exactly what we hand over here.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from nautionette.agent_limits import chat_timeout_exempt

from .. import artcraft, chat_attachments, chat_outputs, mcp_selection, pi_packages
from ..clients import broker
from ..config import settings
from .prompts import CHAT_SYSTEM_PROMPT
from .timing import ActivityTiming

MAX_HISTORY_MESSAGES = 200
# Characters of transcript handed to a cold container. Overridable in settings;
# ~200k chars is roughly 50k tokens, which every current model has room for.
DEFAULT_HISTORY_CHARS = 200_000
MAX_MESSAGE_CHARS = 16_000
TOOL_RESULT_CHARS = 4_000


class Timeline:
    """The ordered story of one answer: the prose, and the tool calls between it.

    Kept so a reloaded chat shows the same interleaving the live stream did.
    """

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.timing = ActivityTiming()
        self._tool_started: dict[str, float] = {}

    def add_text(self, text: str) -> None:
        if not text:
            return
        if self.steps and self.steps[-1]["kind"] == "text":
            self.steps[-1]["text"] += text
        else:
            self.steps.append({"kind": "text", "text": text})

    def start_tool(self, event: dict[str, Any]) -> dict[str, Any]:
        step = {
            "kind": "tool",
            "id": event.get("id") or f"call-{len(self.steps)}",
            "name": event.get("name") or "?",
            "args": event.get("args"),
            "ok": None,
            "result": "",
            "started_at": time.time(),
        }
        self._tool_started[step["id"]] = time.monotonic()
        self.steps.append(step)
        return step

    def finish_tool(self, event: dict[str, Any]) -> None:
        step = self._pending(event.get("id"))
        if step is None:
            step = self.start_tool(event)
            # A missing start event must not invent a zero-length measurement.
            self._tool_started.pop(step["id"], None)
            step.pop("started_at", None)
        self._finish_tool_clock(step)
        step["ok"] = not event.get("error")
        step["result"] = (event.get("result") or "")[:TOOL_RESULT_CHARS]

    def _finish_tool_clock(self, step: dict[str, Any]) -> None:
        start = self._tool_started.pop(step["id"], None)
        step["finished_at"] = time.time()
        if start is not None:
            step["duration_ms"] = round(max(0, time.monotonic() - start) * 1000)

    def stop_tools(self) -> None:
        for step in self.steps:
            if step["kind"] == "tool" and step["ok"] is None and "finished_at" not in step:
                self._finish_tool_clock(step)
                step["interrupted"] = True

    def observe(self, event: dict[str, Any]) -> None:
        self.timing.observe(event, bool(self._tool_started))
        if self.timing.stopped:
            self.stop_tools()

    def _pending(self, call_id: str | None) -> dict[str, Any] | None:
        for step in reversed(self.steps):
            if step["kind"] != "tool" or step["ok"] is not None:
                continue
            if call_id is None or step["id"] == call_id:
                return step
        return None

    @property
    def text(self) -> str:
        return "".join(s["text"] for s in self.steps if s["kind"] == "text").strip()

    @property
    def tools(self) -> list[str]:
        return [s["name"] for s in self.steps if s["kind"] == "tool"]


def build_history(
    messages: list[dict[str, Any]], max_chars: int = DEFAULT_HISTORY_CHARS
) -> list[dict[str, Any]]:
    """Trim a transcript to something a cold container can be handed."""
    trimmed = [m for m in messages if m.get("role") in {"user", "assistant"}]
    trimmed = trimmed[-MAX_HISTORY_MESSAGES:]
    total = 0
    out: list[dict[str, Any]] = []
    remaining_attachments = 4  # Current-turn files are separate; bound replay bytes and context.
    for message in reversed(trimmed):
        content = (message.get("content") or "")[:MAX_MESSAGE_CHARS]
        attachments = (message.get("meta") or {}).get("attachments", []) if message["role"] == "user" else []
        included = attachments[-remaining_attachments:] if remaining_attachments else []
        # File bytes are not model context, only their names/paths; images cost visual context.
        total += len(content) + sum(8000 if chat_attachments.is_image(item) else 512 for item in included)
        if total > max_chars:
            break
        remaining_attachments -= len(included)
        if len(included) < len(attachments):
            content += f"\n[{len(attachments) - len(included)} older attachment(s) omitted from context]"
        entry: dict[str, Any] = {"role": message["role"], "content": content}
        if included:
            entry["attachments"] = included
        out.append(entry)
    return list(reversed(out))


def agent_job(
    *,
    prompt: str,
    mode: str = "interactive",
    system_prompt: str | None = None,
    history: list[dict[str, Any]] | None = None,
    output_schema: dict[str, Any] | None = None,
    agent_set: str | None = None,
    model: str | None = None,
    tools: list[str] | None = None,
    run_id: str = "",
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "prompt": prompt,
        "system_prompt": system_prompt or CHAT_SYSTEM_PROMPT,
        "history": history or [],
        "output_schema": output_schema,
        "agent_set": agent_set or settings.default_agent_set,
        "model": model or settings.agent_model,
        # None means every federated tool; a list narrows the agent to those names.
        "tools": tools,
        "run_id": run_id,
        "timeout_seconds": timeout_seconds,
    }


async def stream_agent(job: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    # Resolve private configuration only in the outgoing call, never in persisted
    # jobs/history, profile snapshots or public API responses.
    from ..db import db

    owner = None
    if job.get("chat_id"):
        owner = db.one(
            "SELECT u.id,u.role,u.status FROM chats c JOIN auth_users u ON u.id=c.owner_user_id WHERE c.id=?",
            (job["chat_id"],),
        )
    elif job.get("run_id"):
        owner = db.one(
            "SELECT u.id,u.role,u.status FROM runs r JOIN auth_users u ON u.id=r.owner_user_id "
            "WHERE r.workflow_id=? OR r.run_id=? LIMIT 1",
            (job["run_id"], job["run_id"]),
        )
    if owner is None and (origin := job.get("origin", {})).get("kind") == "workflow":
        owner = db.one(
            "SELECT u.id,u.role,u.status FROM access_resources r JOIN auth_users u "
            "ON u.id=r.owner_user_id WHERE r.kind='workflow' AND r.resource_id=?",
            (origin.get("workflow"),),
        )
    if owner is None and not job.get("chat_id"):
        from ..access import human_id

        user_id = human_id()
        if not user_id:
            state = db.one("SELECT legacy_owner FROM access_state WHERE id=1")
            user_id = state["legacy_owner"] if state else None
        if user_id:
            owner = db.one("SELECT id,role,status FROM auth_users WHERE id=?", (user_id,))
    if owner and owner["status"] != "active":
        raise PermissionError("The runtime owner is not active")
    member = bool(owner and owner["role"] != "admin")
    if member and (job.get("agent_set") or settings.default_agent_set) != "default":
        raise PermissionError("This container environment is administrator-only")
    # Account authority is resolved here, never trusted from a saved job or a
    # browser payload. Shared member conversations do not imply local code or
    # control-plane access. Full coding remains an administrator capability.
    from ..access import member_tools

    selected_tools = member_tools(job.get("tools"), owner["id"]) if member else job.get("tools")
    outgoing = {
        **job,
        "member_mode": member,
        "tools": selected_tools,
        "artcraft": [] if member else artcraft.for_run(),
        "mcp_targets": await mcp_selection.for_run(selected_tools),
    }
    if member:
        outgoing.update(packages=[], project_credentials=[], package_runtime=[])
    elif job.get("packages"):
        outgoing["package_runtime"] = pi_packages.for_run(job["packages"])
    timeout = None if chat_timeout_exempt(job) else job.get("timeout_seconds", 900) + 30
    from ..access import activity_grant

    with (
        chat_outputs.publication_grant(job.get("chat_id", ""), job.get("turn_id", "")) as chat_grant,
        activity_grant(
            owner["id"] if owner and not chat_grant else None,
            job.get("run_id") or job.get("origin", {}).get("workflow_id", ""),
            selected_tools,
            int(job.get("timeout_seconds") or 900),
        ) as workflow_grant,
    ):
        if grant := chat_grant or workflow_grant:
            outgoing["output_publication"] = grant
        async for event in broker.run_agent(outgoing, timeout=timeout):
            yield event


async def call_agent(
    job: dict[str, Any], *, on_event: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    """Collect a whole agent run into one object. This is what an activity gets."""
    text_parts: list[str] = []
    result: dict[str, Any] | None = None
    error: str | None = None
    tools: list[str] = []

    async for event in stream_agent(job):
        if on_event:
            on_event(event)
        kind = event.get("type")
        if kind == "delta":
            text_parts.append(event.get("text", ""))
        elif kind == "tool":
            tools.append(event.get("name", "?"))
        elif kind == "result":
            result = event
        elif kind == "error":
            error = event.get("message", "agent failed")

    if result is None:
        return {
            "ok": False,
            "text": "".join(text_parts),
            "output": None,
            "error": error or "agent produced no result",
            "tools": tools,
        }
    return {
        "ok": bool(result.get("ok", True)),
        "text": result.get("text") or "".join(text_parts),
        "output": result.get("output"),
        "error": result.get("error"),
        "tools": tools,
        "usage": result.get("usage"),
    }
