#!/usr/bin/python3
"""Bound the first MCP reply from a managed stdio process, including package startup.

This is a byte relay, not an MCP server or a tool proxy. Never retry a child or
record its command, environment, messages or stderr. On EOF or cancellation close
and reap the process group, even if the caller never obtained a gateway session ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import re
import signal
import socket
import sys
import time

OUTCOME_SOCKET = "/tmp/denkzeug-stdio-events.sock"  # noqa: S108 - container-private, 0600
BRAVE_ARGS = ["-y", "@brave/brave-search-mcp-server@2.1.3", "--transport", "stdio"]
BRAVE_ENTRY = "/opt/brave/node_modules/@brave/brave-search-mcp-server/dist/index.js"
BRAVE_PACKAGE = "@brave/brave-search-mcp-server"
STARTUP_SECONDS = 20
MAX_HANDSHAKE_LINE = 2 * 1024 * 1024


def launch(argv: list[str]) -> list[str]:
    if argv[0] in {"npx", "/usr/local/bin/npx"} and argv[1:] in (
        BRAVE_ARGS,
        ["-y", BRAVE_PACKAGE, "--transport", "stdio"],
    ):
        return ["/usr/local/bin/node", BRAVE_ENTRY, "--transport", "stdio"]
    return argv


def publish(target: str, **fields) -> None:
    event = {"target": target, **fields}
    with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        sock.sendto(json.dumps(event).encode(), OUTCOME_SOCKET)


class FirstReply:
    def __init__(self):
        self.request_id = None
        self.method = "unknown"
        self.traceparent = ""
        self.outcome = "ok"
        self.done = asyncio.Event()
        self._incoming = bytearray()
        self._outgoing = bytearray()

    def feed(self, chunk: bytes, *, request: bool) -> None:
        if self.done.is_set() or (request and self.request_id is not None):
            return
        data = self._incoming if request else self._outgoing
        data.extend(chunk)
        while (end := data.find(b"\n")) != -1:
            line = bytes(data[:end])
            del data[: end + 1]
            if len(line) > MAX_HANDSHAKE_LINE:
                continue
            try:
                value = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                continue
            ident = value.get("id")
            if type(ident) not in {int, str}:
                continue
            if request and isinstance(value.get("method"), str):
                self.request_id = ident
                method = value["method"]
                self.method = (
                    method
                    if method in {"initialize", "server/discover", "tools/list", "tools/call"}
                    else "other"
                )
                params = value.get("params")
                meta = params.get("_meta") if isinstance(params, dict) else None
                parent = meta.get("traceparent") if isinstance(meta, dict) else None
                if isinstance(parent, str) and re.fullmatch(
                    r"00-[a-f0-9]{32}-[a-f0-9]{16}-[a-f0-9]{2}", parent
                ):
                    self.traceparent = parent
                break
            if not request and ident == self.request_id and ("result" in value or "error" in value):
                self.outcome = "rpc_error" if "error" in value else "ok"
                self.done.set()
                break
        if len(data) > MAX_HANDSHAKE_LINE or self.done.is_set():
            data.clear()


async def reap(child: asyncio.subprocess.Process) -> None:
    # New process group, so npx/shell grandchildren are included. PID 1 (init)
    # reaps any orphaned grandchildren. Close the pipe first for orderly exits.
    child.stdin.transport.abort()  # no need to flush discarded requests into a hung child
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    # Give descendants a bounded grace period even when their direct parent exits.
    await asyncio.sleep(0.2)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(child.wait(), 1)


async def supervise(target: str, argv: list[str], *, timeout: float = STARTUP_SECONDS) -> int:
    loop = asyncio.get_running_loop()
    started = time.time_ns()
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    # If the gateway itself is killed, terminate even when its pipes are retained
    # by the OS. The parent ID check closes the race with installing the signal.
    parent = os.getppid()
    libc = ctypes.CDLL(None)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # Linux PR_SET_PDEATHSIG
        return 1
    if os.getppid() != parent or parent == 1:
        return 1
    first = FirstReply()

    def report(outcome: str):
        publish(
            target,
            outcome=outcome,
            started_ns=started,
            ended_ns=time.time_ns(),
            deadline_ms=int(timeout * 1000),
            method=first.method,
            traceparent=first.traceparent,
        )

    try:
        child = await asyncio.create_subprocess_exec(  # noqa: S603 - trusted configured argv; never a shell
            *launch(argv),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        report("spawn_error")
        print(f"MCP stdio {target}: spawn_error", file=sys.stderr)
        return 1
    tasks = []
    transports = []
    reported = False
    try:
        stdin = asyncio.StreamReader()
        read_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin.buffer
        )
        transports.append(read_transport)
        write_transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout.buffer
        )
        transports.append(write_transport)
        stdout = asyncio.StreamWriter(write_transport, protocol, None, loop)

        async def relay(source, destination, *, request):
            try:
                while chunk := await source.read(65536):
                    if request:
                        first.feed(chunk, request=True)
                    destination.write(chunk)
                    await destination.drain()
                    if not request:
                        first.feed(chunk, request=False)
            except (ConnectionError, OSError):
                return "pipe_error"
            return "eof"

        tasks = [
            asyncio.create_task(relay(stdin, child.stdin, request=True)),
            asyncio.create_task(relay(child.stdout, stdout, request=False)),
            asyncio.create_task(stopped.wait()),
        ]
        reply = asyncio.create_task(first.done.wait())
        tasks.append(reply)
        completed, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if reply in completed:
            report(first.outcome)
            reported = True
            completed, _ = await asyncio.wait(tasks[:3], return_when=asyncio.FIRST_COMPLETED)
        elif not completed:
            report("timeout")
            reported = True
            print(f"MCP stdio {target}: first reply timed out", file=sys.stderr)
            # Let failOpen skip this target immediately, while keeping the requested
            # operation's outcome unverified if its first request was a tool call.
            if first.request_id is not None:
                with contextlib.suppress(TimeoutError, ConnectionError):
                    async with asyncio.timeout(0.5):
                        stdout.write(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": first.request_id,
                                    "error": {
                                        "code": -32000,
                                        "message": "MCP stdio first reply deadline exceeded",
                                    },
                                }
                            ).encode()
                            + b"\n"
                        )
                        await stdout.drain()
            return 1
        if not reported:
            if stopped.is_set() or tasks[0] in completed:
                outcome = "cancelled"
            else:
                outcome = "pipe_error" if tasks[1].result() == "pipe_error" else "early_exit"
            report(outcome)
            reported = True
    except (ConnectionError, OSError):
        if not reported:
            report("pipe_error")
        return 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await reap(child)
        for transport in transports:
            transport.close()
    return 0


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[2] != "--" or not re.fullmatch(r"[a-zA-Z0-9.-]{1,128}", sys.argv[1]):
        raise SystemExit("usage: stdio_supervisor.py TARGET -- COMMAND [ARG...]")
    raise SystemExit(asyncio.run(supervise(sys.argv[1], sys.argv[3:])))


if __name__ == "__main__":
    main()
