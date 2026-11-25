"""Exercise the real worker and pinned gateway with a hung stdio integration."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from nautionette_backend.mcp_selection import scoped_target
from nautionette_worker import activities
from temporalio.testing import ActivityEnvironment

from tests.agentgateway.test_failure_isolation import CONFIG, McpUpstream, free_port


class Backend(McpUpstream):
    def do_DELETE(self):
        self.server.closed.append(self.path)
        self.send_response(204)
        self.end_headers()

    def reply(self, status, value):
        if self.path in {"/backend", "/workflows"} and status == 200:
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if "protocolVersion" in value.get("result", {}):
                self.send_header("Mcp-Session-Id", f"fixture-{self.path[1:]}")
            self.end_headers()
            self.wfile.write(body)
        else:
            super().reply(status, value)

    def do_POST(self):
        if not self.path.startswith("/internal/"):
            return super().do_POST()
        assert self.headers.get("X-Internal-Token") == "local-worker-token"
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.worker_requests.append(self.path)
        if self.path.endswith("/activity-grant"):
            return self.reply(200, {"grant": "fixture-actor", "read_only": True})
        return self.reply(200, {"ok": True})


@pytest.mark.slow
async def test_pinned_gateway_worker_avoids_hung_stdio_and_unscoped_gateway_reaps_it(tmp_path, monkeypatch):
    binary = os.environ.get("NAUTIONETTE_TEST_GATEWAY_BINARY")
    if not binary:
        pytest.skip("Set NAUTIONETTE_TEST_GATEWAY_BINARY for integration coverage")
    source = CONFIG.parents[1] / "stdio_supervisor.py"
    wrapper = tmp_path / "supervisor"
    wrapper.write_text(
        source.read_text()
        .replace("#!/usr/bin/python3", f"#!{sys.executable}", 1)
        .replace("STARTUP_SECONDS = 20", "STARTUP_SECONDS = 1.2")
    )
    wrapper.chmod(0o755)
    pid_path = tmp_path / "pids"
    child = tmp_path / "never_ready.py"
    child.write_text(
        "import os, pathlib, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {grandchild.pid}')\n"
        "time.sleep(60)\n"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    server.requests, server.calls, server.worker_requests, server.closed = [], [], [], []
    server.all_broken = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    backend = f"http://127.0.0.1:{server.server_port}"
    config = yaml.safe_load(CONFIG.read_text())
    config["config"].update(
        database={"url": f"sqlite:///{tmp_path}/gateway.db"},
        adminAddr="127.0.0.1:0",
        readinessAddr="127.0.0.1:0",
        statsAddr="127.0.0.1:0",
    )
    config["gateways"]["default"]["port"] = port
    config["mcp"]["targets"] = [
        {
            "name": name,
            "mcp": {"host": backend + "/" + name},
            "policies": {"backendAuth": {"key": {"value": "local-test-token"}}},
        }
        for name in ("workflows", "backend")
    ] + [
        scoped_target(
            {
                "name": "hung",
                "stdio": {
                    "cmd": str(wrapper),
                    "args": ["hung", "--", sys.executable, str(child), str(pid_path)],
                    "clear_env": True,
                },
            }
        )
    ]
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(activities, "BACKEND_URL", backend)
    monkeypatch.setattr(activities, "INTERNAL_TOKEN", "local-worker-token")
    monkeypatch.setattr(activities, "MCP_URL", base + "/mcp")
    env = ActivityEnvironment()
    env.info = replace(
        env.info, workflow_id="fixture", start_to_close_timeout=None, schedule_to_close_timeout=None
    )

    def alive(pid):
        path = Path(f"/proc/{pid}/stat")
        return path.exists() and path.read_text().rsplit(")", 1)[-1].split()[0] != "Z"

    log_path = tmp_path / "gateway.log"
    try:
        with log_path.open("w") as log:
            proc = subprocess.Popen([binary, "-f", str(path)], stdout=log, stderr=log)  # noqa: S603 - opt-in pinned fixture
            try:
                async with httpx.AsyncClient(base_url=base, timeout=4, trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if (await client.get("/api/runtime")).is_success:
                                break
                        except httpx.HTTPError:
                            pass
                        assert proc.poll() is None, log_path.read_text()
                        await asyncio.sleep(0.05)
                    else:
                        pytest.fail("gateway did not start")
                    for _ in range(4):
                        async with asyncio.timeout(4):
                            result = await env.run(activities.mcp_call, {"tool": "backend_read"})
                        assert result["text"] == "healthy result"
                    assert not pid_path.exists()
                    assert server.calls == ["/backend"] * 4
                    assert server.closed.count("/backend") == 4
                    assert server.worker_requests.count("/internal/auth/revoke-activity-grant") == 4
                    # Unscoped by design: failOpen must skip a child that never
                    # replies when the supervisor's internal deadline fires.
                    headers = {
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2025-06-18",
                    }
                    response = await client.post(
                        "/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "fixture", "version": "1"},
                            },
                        },
                    )
                    response.raise_for_status()
                    assert activities._mcp_frame(response, 1)["protocolVersion"] == "2025-06-18"
                    pids = [int(pid) for pid in pid_path.read_text().split()]
                    for _ in range(30):
                        if not any(alive(pid) for pid in pids):
                            break
                        await asyncio.sleep(0.1)
                    assert not any(alive(pid) for pid in pids), "unreferenced child group remained alive"
                    assert "first reply timed out" in log_path.read_text()
                    headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
                    assert (await client.delete("/mcp", headers=headers)).is_success
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
