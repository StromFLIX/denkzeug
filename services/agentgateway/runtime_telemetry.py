"""Fixed-schema, best-effort startup and pressure telemetry for the gateway image.

Runs with the gateway UID, not the broker's privileges. No external dependencies,
Docker socket, credentials, command lines, tool arguments, or raw process output.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import time
import urllib.request
from pathlib import Path

from stdio_supervisor import OUTCOME_SOCKET

OUTCOMES = {"ok", "timeout", "spawn_error", "early_exit", "cancelled", "rpc_error", "pipe_error"}
METHODS = {"unknown", "initialize", "server/discover", "tools/list", "tools/call", "other"}


def attrs(values: dict) -> list[dict]:
    return [
        {"key": key, "value": {"intValue": str(value)} if type(value) is int else {"stringValue": str(value)}}
        for key, value in values.items()
    ]


def startup_span(event: dict) -> dict | None:
    target, outcome, method = (event.get(key) for key in ("target", "outcome", "method"))
    if not isinstance(target, str) or not re.fullmatch(r"[a-zA-Z0-9.-]{1,128}", target):
        return None
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        return None
    if not isinstance(method, str) or method not in METHODS:
        return None
    started, ended, budget = (event.get(key) for key in ("started_ns", "ended_ns", "deadline_ms"))
    if not all(type(value) is int for value in (started, ended, budget)) or not (
        0 < started <= ended and 0 < budget < 120000
    ):
        return None
    parent = event.get("traceparent")
    match = (
        re.fullmatch(r"00-([a-f0-9]{32})-([a-f0-9]{16})-[a-f0-9]{2}", parent)
        if isinstance(parent, str)
        else None
    )
    identifiers = (
        {"traceId": match[1], "parentSpanId": match[2]}
        if match and int(match[1], 16) and int(match[2], 16)
        else {"traceId": os.urandom(16).hex()}
    )
    labels = {
        "rpc.system": "mcp",
        "rpc.method": method,
        "denkzeug.mcp.stage": "first_reply",
        "denkzeug.mcp.target": target,
        "denkzeug.mcp.outcome": outcome,
        "denkzeug.mcp.deadline_ms": budget,
    }
    status = {"code": 1} if outcome == "ok" else {"code": 0} if outcome == "cancelled" else {"code": 2}
    if status["code"] == 2:
        labels["error.type"] = "mcp.stdio." + outcome
    return {
        **identifiers,
        "spanId": os.urandom(8).hex(),
        "name": "mcp.stdio.first_reply",
        "kind": 3,
        "startTimeUnixNano": str(started),
        "endTimeUnixNano": str(ended),
        "attributes": attrs(labels),
        "status": status,
    }


def pressure() -> dict[str, tuple[str, float | int]]:
    readings = {}
    for scope, path in (("host", "/proc/pressure/memory"), ("container", "/sys/fs/cgroup/memory.pressure")):
        with contextlib.suppress(OSError, KeyError, ValueError):
            for line in Path(path).read_text().splitlines():
                kind, *fields = line.split()
                if kind in {"some", "full"}:
                    readings[f"denkzeug.{scope}.memory.pressure.{kind}.avg10"] = (
                        "%",
                        float(dict(part.split("=", 1) for part in fields)["avg10"]),
                    )
    with contextlib.suppress(OSError, ValueError):
        readings["denkzeug.container.memory.current"] = (
            "By",
            int(Path("/sys/fs/cgroup/memory.current").read_text()),
        )
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines():
            name, value = line.split()
            if name in {"oom", "oom_kill", "high", "max"}:
                readings[f"denkzeug.container.memory.events.{name}"] = ("{event}", int(value))
    return readings


def live_managed_children(proc: Path = Path("/proc")) -> int:
    """The supervisor starts each child with setsid(); count live group leaders.

    Recount at export rather than trusting possibly dropped Unix datagrams from
    short-lived launchers. Inspect only numeric stat fields and executable names.
    """
    parents, leaders = set(), []
    for path in proc.glob("[0-9]*/stat"):
        try:
            pid = int(path.parent.name)
            _, _, suffix = path.read_text().rpartition(")")
            state, ppid, pgid, *_ = suffix.split()
            if state == "Z":
                continue
            if pid == int(pgid):
                leaders.append(int(ppid))
            # Linux comm is truncated to 15 bytes on scripts.
            if (path.parent / "comm").read_text().strip() == "stdio_superviso":
                parents.add(pid)
        except (OSError, ValueError):
            continue
    return sum(parent in parents for parent in leaders)


def gauge(name: str, unit: str, value: int | float) -> dict:
    number = {"asInt": str(value)} if type(value) is int else {"asDouble": value}
    return {
        "name": name,
        "unit": unit,
        "gauge": {"dataPoints": [{"timeUnixNano": str(time.time_ns()), **number}]},
    }


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main() -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if not endpoint or os.environ.get("OTEL_SDK_DISABLED", "true").lower() == "true":
        return
    resource = {
        "attributes": attrs(
            {
                "service.name": "nautionette-gateway-runtime",
                "service.namespace": "nautionette",
                "service.version": os.environ.get("APP_VERSION", "dev"),
                "deployment.environment": os.environ.get("APP_ENVIRONMENT", "production"),
            }
        )
    }
    scope = {"name": "denkzeug.gateway.runtime"}
    opener = urllib.request.build_opener(NoRedirects())
    # Never put exporter credentials into the wrapper's child environment (the
    # MCP runtime clears it) or attach them to a redirect.
    from urllib.parse import unquote

    headers = {
        key.strip(): unquote(value.strip())
        for field in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(",")
        for key, separator, value in [field.partition("=")]
        if separator and key.strip()
    }
    headers["Content-Type"] = "application/json"

    def send(signal: str, key: str, values: list[dict]) -> None:
        if not values:
            return
        label = signal.capitalize()
        payload = {
            "resource" + label: [{"resource": resource, "scope" + label: [{"scope": scope, key: values}]}]
        }
        with contextlib.suppress(OSError):
            req = urllib.request.Request(  # noqa: S310 - operator-configured OTLP endpoint
                endpoint + "/v1/" + signal,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            with opener.open(req, timeout=2):
                pass

    path = Path(OUTCOME_SOCKET)
    path.unlink(missing_ok=True)
    spans: list[dict] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(OUTCOME_SOCKET)
        path.chmod(0o600)
        receiver.settimeout(1)
        next_flush = time.monotonic()
        try:
            while True:
                with contextlib.suppress(TimeoutError, ValueError, TypeError, RecursionError):
                    event = json.loads(receiver.recv(4096))
                    if isinstance(event, dict):
                        if len(spans) < 256 and (span := startup_span(event)):
                            spans.append(span)
                if time.monotonic() < next_flush:
                    continue
                send("traces", "spans", spans)
                spans.clear()
                readings = pressure()
                readings["denkzeug.gateway.stdio.children"] = ("{process}", live_managed_children())
                send(
                    "metrics",
                    "metrics",
                    [gauge(name, unit, value) for name, (unit, value) in readings.items()],
                )
                next_flush = time.monotonic() + 10
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
