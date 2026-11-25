"""The activities every workflow can call by name.

A workflow file imports nothing from the runtime: it calls "agent_call",
"http_fetch", "mcp_call", "emit_event", "save_artifact" or "read_artifact" as
strings. All of them are deterministic except "agent_call".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jsonschema
from opentelemetry import metrics, trace
from temporalio import activity
from temporalio.exceptions import ApplicationError

log = logging.getLogger("worker.activities")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8080").rstrip("/")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "").strip()
MCP_URL = os.environ.get("MCP_URL", "http://agentgateway:4000/mcp")
ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
AGENT_TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "900"))
HTTP_MAX_BYTES = 512_000
MCP_SCOPE_HEADER = "x-nautionette-mcp-targets"
MCP_FIRST_PARTY_INIT_SECONDS = 15
MCP_INIT_SECONDS = 45
MCP_CALL_SECONDS = 120
MCP_CLEANUP_SECONDS = 3
MCP_OBSERVE_SECONDS = 2
MCP_CLEANUP_RESERVE_SECONDS = 9
_MCP_METER = metrics.get_meter(__name__)
_MCP_PHASE_DURATION = _MCP_METER.create_histogram("denkzeug.workflow.mcp.phase.duration", unit="s")
_MCP_HELD_SESSIONS = _MCP_METER.create_up_down_counter(
    "denkzeug.workflow.mcp.sessions.held", unit="{session}"
)
_MCP_TRACER = trace.get_tracer(__name__)
_mcp_targets_cache: tuple[float, frozenset[str]] = (0, frozenset())


def _headers() -> dict[str, str]:
    return {"X-Internal-Token": INTERNAL_TOKEN} if INTERNAL_TOKEN else {}


@activity.defn(name="agent_call")
async def agent_call(params: dict[str, Any]) -> dict[str, Any]:
    """One agent step. Structured output in, one JSON object out."""
    info = activity.info()
    if "agent_id" in params:
        try:
            return await _profile_agent_call(params)
        except asyncio.CancelledError:
            # Worker redeployment reconnects; an explicit Temporal cancellation
            # requests a normal chat stop. Abrupt workflow termination cannot
            # deliver cancellation and still requires inspecting the linked chat.
            if (
                activity.is_cancelled()
                and not activity.is_worker_shutdown()
                and not params.get("validate_only")
            ):
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        stopped = await client.delete(_profile_task_url(), headers=_headers())
                        stopped.raise_for_status()
                except Exception:  # noqa: BLE001 - preserve cancellation, report uncertain cleanup
                    log.exception("Could not stop cancelled saved-agent task %s", _profile_task_url())
            raise
    payload = {
        "prompt": params.get("prompt", ""),
        "system_prompt": params.get("system_prompt"),
        "history": params.get("history") or [],
        "output_schema": params.get("output_schema"),
        "agent_set": params.get("agent_set"),
        "run_id": info.workflow_id,
        "source": _workflow_source(),
        "timeout_seconds": int(params.get("timeout_seconds") or AGENT_TIMEOUT_SECONDS),
    }
    timeout = payload["timeout_seconds"] + 60
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10)) as client:
        response = await client.post(f"{BACKEND_URL}/internal/agent/call", json=payload, headers=_headers())
        response.raise_for_status()
        result = response.json()

    if not result.get("ok"):
        # Fail the activity so Temporal retries it, instead of passing junk downstream.
        raise RuntimeError(result.get("error") or "agent call failed")
    schema = params.get("output_schema")
    if schema and result.get("output") is None:
        raise RuntimeError("agent returned text where a structured object was declared")
    return result


def _workflow_source() -> dict[str, str]:
    info = activity.info()
    return {
        "workflow": info.workflow_type,
        "workflow_id": info.workflow_id,
        "run_id": info.workflow_run_id,
        "activity_id": info.activity_id,
    }


def _profile_task_url() -> str:
    info = activity.info()
    key = hashlib.sha256(f"{info.workflow_run_id}:{info.activity_id}".encode()).hexdigest()
    return f"{BACKEND_URL}/internal/agent/tasks/{key}"


async def _profile_agent_call(params: dict[str, Any]) -> dict[str, Any]:
    """Reconnect to one durable chat turn instead of replaying side effects.

    This key excludes the activity attempt, but includes the workflow run ID so
    the next scheduled execution starts a fresh task. Profile settings/projects
    are snapshotted at admission by the backend, not copied from workflow input.
    """
    url = _profile_task_url()
    deadline = time.monotonic() + int(params.get("timeout_seconds") or AGENT_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        response = await client.post(
            url,
            json={
                "agent_id": params["agent_id"],
                "prompt": params.get("prompt", ""),
                "validate_only": params.get("validate_only", False),
                "source": _workflow_source(),
            },
            headers=_headers(),
        )
        response.raise_for_status()
        result = response.json()
        if params.get("validate_only"):
            if not result.get("validated") or not result.get("ok"):
                raise RuntimeError("Saved agent profile validation failed")
            return result
        while "ok" not in result:
            if time.monotonic() >= deadline:
                stopped = await client.delete(url, headers=_headers())
                stopped.raise_for_status()
                raise TimeoutError(
                    f"Saved agent task timed out; stop requested for chat {result.get('chat_id')}"
                )
            activity.heartbeat({"chat_id": result.get("chat_id"), "turn_id": result.get("turn_id")})
            await asyncio.sleep(10)
            response = await client.get(url, headers=_headers())
            response.raise_for_status()
            result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "Saved agent task failed")
    schema = params.get("output_schema")
    if schema:
        text = result.get("text", "").strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        output = json.loads(text)
        jsonschema.validate(output, schema)
        result["output"] = output
    return result


@activity.defn(name="http_fetch")
async def http_fetch(params: dict[str, Any]) -> dict[str, Any]:
    url = params.get("url")
    if not url or not str(url).startswith(("http://", "https://")):
        raise ValueError("http_fetch needs an http(s) url")
    method = (params.get("method") or "GET").upper()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.request(
            method,
            url,
            headers=params.get("headers") or {},
            json=params.get("json"),
            params=params.get("params"),
        )
    body = response.text[:HTTP_MAX_BYTES]
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    return {"status": response.status_code, "body": body, "json": parsed}


@activity.defn(name="emit_event")
async def emit_event(params: dict[str, Any]) -> dict[str, Any]:
    """Progress goes to the backend, which is what the clients are watching."""
    info = activity.info()
    payload = {
        "scope": "workflow",
        "kind": params.get("kind") or "workflow.event",
        "payload": {"workflow_id": info.workflow_id, **(params.get("payload") or {})},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{BACKEND_URL}/internal/events", json=payload, headers=_headers())
    except Exception as exc:  # noqa: BLE001 - never fail a run because a notice did not land
        log.warning("emit_event failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": True}


def _artifact_path(name: str) -> Path:
    safe = os.path.basename(str(name)).strip() or "artifact.txt"
    return ARTIFACTS_DIR / safe


@activity.defn(name="save_artifact")
async def save_artifact(params: dict[str, Any]) -> dict[str, Any]:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _artifact_path(params.get("name", "artifact.txt"))
    path.write_text(str(params.get("content", "")), encoding="utf-8")
    return {"path": str(path), "name": path.name, "bytes": path.stat().st_size}


@activity.defn(name="read_artifact")
async def read_artifact(params: dict[str, Any]) -> dict[str, Any]:
    path = _artifact_path(params.get("name", ""))
    if not path.is_file():
        raise FileNotFoundError(f"artifact '{path.name}' does not exist")
    return {"name": path.name, "content": path.read_text(encoding="utf-8", errors="replace")}


def _mcp_frame(response: httpx.Response, request_id: int) -> dict[str, Any]:
    """Ignore interleaved notifications; accept only this request's terminal reply."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        bodies = (
            "\n".join(line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:"))
            for frame in response.text.replace("\r\n", "\n").split("\n\n")
        )
    else:
        bodies = (response.text,)
    for body in bodies:
        if body and isinstance(value := json.loads(body), dict) and value.get("id") == request_id:
            if value.get("error") is not None:
                raise _MCPResponseError("MCPRPCError")
            if isinstance(value.get("result"), dict):
                return value["result"]
    raise _MCPResponseError("MCPInvalidResponse")


class _MCPResponseError(Exception):
    pass


def _rpc(method: str, request_id: int | None = None, **params: Any) -> dict:
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        body["id"] = request_id
    return body


async def _mcp_target(client: httpx.AsyncClient, tool: str, first_party: str | None) -> str | None:
    """Resolve normal federated names without opening an MCP catalog session."""
    global _mcp_targets_cache
    if first_party:
        return first_party
    prefix, separator, _ = tool.partition("_")
    if not separator:
        return None
    expires, names = _mcp_targets_cache
    if time.monotonic() >= expires:
        # Older/self-hosted backends may not have this optional routing helper.
        try:
            async with asyncio.timeout(2):
                response = await client.get(f"{BACKEND_URL}/internal/mcp/targets", headers=_headers())
                response.raise_for_status()
                values = response.json()["targets"]
                if not isinstance(values, list) or not all(
                    isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,128}", name) for name in values
                ):
                    raise _MCPResponseError("MCPInvalidTargetCatalog")
                names = frozenset(values)
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, _MCPResponseError):
            names = frozenset()
            log.warning("MCP target catalog unavailable; using bounded compatibility handshake")
        _mcp_targets_cache = (time.monotonic() + (10 if names else 2), names)
    # The backend omits raw-name catalogs. An unrecognized name is left alone.
    return prefix if prefix in names else None


def _mcp_deadline(info: activity.Info | None) -> float:
    """Reserve time to close sessions and report results before Temporal expires."""
    now = datetime.now(UTC)
    deadlines = []
    if info:
        if info.start_to_close_timeout:
            deadlines.append(info.started_time + info.start_to_close_timeout)
        if info.schedule_to_close_timeout:
            deadlines.append(info.scheduled_time + info.schedule_to_close_timeout)
    remaining = (min(deadlines) - now).total_seconds() if deadlines else float("inf")
    return time.monotonic() + remaining - MCP_CLEANUP_RESERVE_SECONDS


def _mcp_budget(deadline: float, seconds: float) -> float:
    return max(0, min(seconds, deadline - time.monotonic()))


@contextmanager
def _mcp_phase(phase: str, *, tool: str, target: str | None, attempt: int, budget: float):
    start, outcome = time.monotonic(), "ok"
    with _MCP_TRACER.start_as_current_span(
        f"workflow.mcp.{phase}",
        attributes={
            "rpc.system": "mcp",
            "denkzeug.mcp.stage": phase,
            "denkzeug.mcp.target": target or "unscoped",
            "denkzeug.mcp.deadline_ms": int(budget * 1000),
            "gen_ai.tool.name": tool,
            "temporalAttempt": attempt,
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield
        except (TimeoutError, httpx.TimeoutException):
            outcome = "timeout"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            span.set_attribute("denkzeug.mcp.outcome", outcome)
            if outcome in {"timeout", "error"}:
                span.set_attribute("error.type", f"mcp.{phase}.{outcome}")
                span.set_status(trace.StatusCode.ERROR)
            _MCP_PHASE_DURATION.record(time.monotonic() - start, {"stage": phase, "outcome": outcome})


async def _observe_mcp(client: httpx.AsyncClient, info: activity.Info | None, tool: str, status: str):
    if not info or not info.workflow_id:
        return
    try:
        async with asyncio.timeout(MCP_OBSERVE_SECONDS):
            response = await client.post(
                f"{BACKEND_URL}/internal/runs/{quote(info.workflow_id, safe='')}/mcp-calls",
                json={
                    "call_id": f"{info.workflow_run_id}:{info.activity_id}:{info.attempt}",
                    "tool": tool,
                    "status": status,
                },
                headers=_headers(),
            )
            response.raise_for_status()
    except (TimeoutError, httpx.HTTPError):
        log.warning("Could not record MCP call statistics for activity %s", info.activity_id)


@activity.defn(name="mcp_call")
async def mcp_call(params: dict[str, Any]) -> dict[str, Any]:
    """One scoped call; never replay a dispatch with an unknown outcome."""
    tool = params.get("tool")
    if not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", tool):
        raise ValueError("mcp_call needs a tool name")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    read_only = params.get("read_only", False)
    if not isinstance(arguments, dict) or type(read_only) is not bool:
        raise ValueError("mcp_call expects an arguments object and a boolean read_only flag")
    first_party = next((name for name in ("backend", "workflows") if tool.startswith(name + "_")), None)
    info = activity.info() if activity.in_activity() else None
    if first_party and not info:
        raise ValueError("First-party MCP calls require verified workflow activity provenance")
    attempt = info.attempt if info else 1
    deadline = _mcp_deadline(info)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    status, phase = "interrupted", "target_selection"
    target = first_party
    held = dispatched = False
    grant = None
    async with httpx.AsyncClient(timeout=MCP_CALL_SECONDS) as client:

        async def cleanup(stage: str, request) -> None:
            try:
                with _mcp_phase(stage, tool=tool, target=target, attempt=attempt, budget=MCP_CLEANUP_SECONDS):
                    async with asyncio.timeout(MCP_CLEANUP_SECONDS):
                        response = await request()
                        if stage != "session_delete" or response.status_code != 404:
                            response.raise_for_status()
            except Exception:  # noqa: BLE001 - cleanup must not fail or replay a completed tool
                log.warning("MCP cleanup incomplete: stage=%s tool=%s attempt=%s", stage, tool, attempt)

        try:
            await _observe_mcp(client, info, tool, "running")
            budget = _mcp_budget(deadline, 3)
            with _mcp_phase(phase, tool=tool, target=target, attempt=attempt, budget=budget):
                async with asyncio.timeout(budget):
                    target = await _mcp_target(client, tool, first_party)
            if target is not None:
                headers[MCP_SCOPE_HEADER] = target
            phase = "initialize"
            init_deadline = time.monotonic() + _mcp_budget(
                deadline,
                MCP_FIRST_PARTY_INIT_SECONDS if first_party else MCP_INIT_SECONDS,
            )
            budget = _mcp_budget(init_deadline, MCP_INIT_SECONDS)
            with _mcp_phase(phase, tool=tool, target=target, attempt=attempt, budget=budget):
                async with asyncio.timeout(budget):
                    if not budget:
                        raise TimeoutError
                    request = client.build_request(
                        "POST",
                        MCP_URL,
                        headers=headers,
                        json=_rpc(
                            "initialize",
                            1,
                            protocolVersion=headers["MCP-Protocol-Version"],
                            capabilities={},
                            clientInfo={"name": "nautionette-workflow", "version": "1"},
                        ),
                    )
                    handshake = await client.send(request, stream=True)
                    try:
                        # Catch even a response whose headers arrive but whose
                        # SSE body never finishes. We can already close its ID.
                        if session := handshake.headers.get("mcp-session-id"):
                            headers["Mcp-Session-Id"] = session
                            held = True
                            _MCP_HELD_SESSIONS.add(1)
                        handshake.raise_for_status()
                        await handshake.aread()
                        version = _mcp_frame(handshake, 1).get("protocolVersion")
                    finally:
                        await handshake.aclose()
                    if not isinstance(version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
                        raise _MCPResponseError("MCPInvalidResponse")
                    headers["MCP-Protocol-Version"] = version
            phase = "initialized_notification"
            budget = _mcp_budget(init_deadline, MCP_INIT_SECONDS)
            with _mcp_phase(phase, tool=tool, target=target, attempt=attempt, budget=budget):
                async with asyncio.timeout(budget):
                    if not budget:
                        raise TimeoutError
                    notice = await client.post(
                        MCP_URL, headers=headers, json=_rpc("notifications/initialized")
                    )
                    notice.raise_for_status()
            if first_party:
                phase = "activity_grant"
                budget = _mcp_budget(deadline, 10)
                with _mcp_phase(phase, tool=tool, target=target, attempt=attempt, budget=budget):
                    async with asyncio.timeout(budget):
                        proof = await client.post(
                            f"{BACKEND_URL}/internal/auth/activity-grant",
                            headers=_headers(),
                            json={"source": _workflow_source(), "tool": tool},
                        )
                        proof.raise_for_status()
                        authority = proof.json()
                        grant = authority["grant"]
                        # The server classifies its own API operations; don't allow
                        # a caller-supplied read_only flag to mark a first-party write.
                        read_only = authority.get("read_only") is True
                        arguments = {**arguments, "__denkzeug_actor": grant}
            phase = "tools_call"
            budget = _mcp_budget(deadline, MCP_CALL_SECONDS)
            with _mcp_phase(phase, tool=tool, target=target, attempt=attempt, budget=budget):
                async with asyncio.timeout(budget):
                    if budget <= 0:
                        raise TimeoutError
                    dispatched = True
                    called = await client.post(
                        MCP_URL,
                        headers=headers,
                        json=_rpc(
                            "tools/call",
                            2,
                            name=tool,
                            arguments=arguments,
                        ),
                    )
                    called.raise_for_status()
                    result = _mcp_frame(called, 2)
                    if result.get("isError"):
                        raise _MCPResponseError("MCPToolError")
                    text = "\n".join(
                        part.get("text", "")
                        for part in result.get("content", [])
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                    try:
                        parsed = json.loads(text) if text else None
                    except ValueError:
                        parsed = None
            status = "completed"
            return {"ok": True, "text": text, "json": parsed}
        except (httpx.HTTPError, TimeoutError, _MCPResponseError, ValueError, TypeError, KeyError) as exc:
            status = "failed"
            kind = {
                "initialize": "MCPInitialize",
                "initialized_notification": "MCPInitializedNotification",
                "tools_call": "MCPCall",
                "activity_grant": "MCPActivityGrant",
                "target_selection": "MCPTargetSelection",
            }[phase]
            if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                error_type, reason = kind + "Timeout", f"deadline {budget:.1f}s exceeded"
            elif isinstance(exc, httpx.HTTPStatusError):
                error_type, reason = kind + "HTTPError", f"HTTP {exc.response.status_code}"
            elif isinstance(exc, httpx.HTTPError):
                error_type, reason = kind + "TransportError", type(exc).__name__
            else:
                error_type = str(exc) if isinstance(exc, _MCPResponseError) else "MCPInvalidResponse"
                reason = error_type
            dispatch = "tool outcome unverified" if dispatched else "tool not dispatched"
            trace_id = trace.get_current_span().get_span_context().trace_id
            correlation = f"; trace {trace_id:032x}" if trace_id else ""
            raise ApplicationError(
                f"MCP {phase} failed: {tool} ({reason}; attempt {attempt}; {dispatch}){correlation}",
                type=error_type,
                non_retryable=(
                    dispatched
                    and not read_only
                    or isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code in {400, 401, 403, 422}
                ),
            ) from None
        finally:
            try:
                try:
                    if grant:
                        # Fence account access before ending the gateway session.
                        await cleanup(
                            "grant_revoke",
                            lambda: client.post(
                                f"{BACKEND_URL}/internal/auth/revoke-activity-grant",
                                headers=_headers(),
                                json={"grant": grant},
                            ),
                        )
                finally:
                    if held:
                        await cleanup("session_delete", lambda: client.delete(MCP_URL, headers=headers))
                await _observe_mcp(client, info, tool, status)
            finally:
                if held:
                    _MCP_HELD_SESSIONS.add(-1)


ALL = [agent_call, http_fetch, mcp_call, emit_event, save_artifact, read_artifact]
