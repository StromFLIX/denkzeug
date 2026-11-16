"""Prove the shipped ceiling reaches HTTP MCP subrequests, not merely the route."""

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from nautionette_backend.clients.agentgateway import GatewayClient
from nautionette_backend.mcp_timeouts import (
    GATEWAY_REQUEST_TIMEOUT,
    logical_http_timeout,
    with_http_timeout,
)

ROOT = Path(__file__).resolve().parents[2]


def test_timeout_defaults_preserve_every_other_target_setting():
    original = {
        "name": "fixture",
        "mcp": {"host": "https://example.test/mcp"},
        "condition": "false",
        "policies": {
            "backendAuth": {"key": {"value": "fixture-secret"}},
            "http": {"version": "HTTP/1.1"},
            "transformations": {"request": {}},
        },
    }
    before = deepcopy(original)
    result = with_http_timeout(original)
    assert result["policies"]["http"]["requestTimeout"] == "615s"
    assert logical_http_timeout(result) == original  # Preserve pre-upgrade OAuth snapshots.
    result["policies"]["http"].pop("requestTimeout")
    assert result == original == before
    original["policies"]["http"]["requestTimeout"] = "20s"
    assert with_http_timeout(original) == original  # Explicit admin limits win.
    assert logical_http_timeout(original) == original
    stdio = {"name": "stdio", "stdio": {"cmd": "fixture", "env": {"FIXTURE": "secret"}}}
    assert with_http_timeout(stdio) == stdio  # HTTP policies are invalid for stdio.
    assert with_http_timeout({"mcp": {}, "policies": None})["policies"]["http"]["requestTimeout"] == "615s"
    config = yaml.safe_load((ROOT / "services/agentgateway/config/config.yaml").read_text())
    assert config["mcp"]["targets"][0]["policies"]["http"]["requestTimeout"] == GATEWAY_REQUEST_TIMEOUT
    assert "timeout" not in config.get("llm", {})


class SlowMcp(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/runtime" and self.headers.get("Authorization") != "Bearer local-test-token":
            return self.reply(401, {})
        method = body["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "slow-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "wait",
                        "inputSchema": {"type": "object"},
                        "annotations": {"readOnlyHint": False},
                    }
                ]
            }
        elif method == "tools/call":
            self.server.calls.append(self.path)
            time.sleep(10.2)  # Deliberately exceeds the old, hidden 10s hop limit.
            result = {"content": [{"type": "text", "text": "completed once"}]}
        else:
            return self.reply(202, {})
        return self.reply(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": result})


@pytest.mark.slow
async def test_real_gateway_baseline_and_runtime_calls_survive_old_ten_second_limit(tmp_path):
    binary = os.environ.get("NAUTIONETTE_TEST_GATEWAY_BINARY")
    if not binary:
        pytest.skip("Set NAUTIONETTE_TEST_GATEWAY_BINARY for real MCP deadline regression")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowMcp)
    upstream.calls = []
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    config = yaml.safe_load((ROOT / "services/agentgateway/config/config.yaml").read_text())
    config["config"].update(
        database={"url": f"sqlite:///{tmp_path}/gateway.db"},
        adminAddr="127.0.0.1:0",
        readinessAddr="127.0.0.1:0",
        statsAddr="127.0.0.1:0",
    )
    config["gateways"]["default"]["port"] = port
    config["mcp"]["targets"][0]["mcp"]["host"] = f"http://127.0.0.1:{upstream.server_port}/baseline"
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(config))
    process = None
    try:
        with (tmp_path / "gateway.log").open("w") as log:
            process = subprocess.Popen([binary, "-f", str(path)], stdout=log, stderr=log)  # noqa: S603
            async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as client:
                for _ in range(100):
                    assert process.poll() is None, (tmp_path / "gateway.log").read_text()
                    try:
                        if (await client.get("/api/runtime")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                runtime = with_http_timeout(
                    {
                        "name": "runtime",
                        "mcp": {"host": f"http://127.0.0.1:{upstream.server_port}/runtime"},
                        "policies": {"backendAuth": {"key": {"value": "local-test-token"}}},
                    }
                )
                response = await client.put(
                    "/api/config/resources/mcp.target", json={"resources": [{"value": runtime}]}
                )
                response.raise_for_status()
                for _ in range(50):
                    tools = await GatewayClient(base).mcp_tools(base + "/mcp", timeout=5)
                    if len(tools) == 2:
                        break
                    await asyncio.sleep(0.1)
                assert {tool["name"] for tool in tools} == {"workflows_wait", "runtime_wait"}
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
                            "clientInfo": {"name": "deadline-test", "version": "1"},
                        },
                    },
                )
                response.raise_for_status()
                headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
                for request_id, tool in enumerate(("workflows_wait", "runtime_wait"), 2):
                    response = await client.post(
                        "/mcp",
                        headers=headers,
                        json={
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "tools/call",
                            "params": {"name": tool, "arguments": {}},
                        },
                    )
                    response.raise_for_status()
                    assert "completed once" in response.text
                assert upstream.calls == ["/baseline", "/runtime"]
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=15)
        upstream.shutdown()
        thread.join(timeout=5)
        upstream.server_close()
