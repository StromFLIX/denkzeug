"""The pinned real gateway must preserve hidden capability arguments unchanged."""

import asyncio
import json
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from nautionette_backend.clients.agentgateway import _rpc_result

from tests.agentgateway.test_failure_isolation import CONFIG, McpUpstream, free_port


class ActorUpstream(McpUpstream):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer local-test-token":
            return self.reply(401, {"error": "transport credential required"})
        method = body["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "actor-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "call",
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            self.server.arguments.append(body["params"]["arguments"])
            result = {"content": [{"type": "text", "text": "scope received"}]}
        else:
            return self.reply(202, {})
        return self.reply(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": result})


@pytest.mark.slow
async def test_real_gateway_preserves_account_capabilities_with_strict_model_schemas(tmp_path):
    binary = os.environ.get("NAUTIONETTE_TEST_GATEWAY_BINARY")
    if not binary:
        pytest.skip("Requires the checksum-pinned agentgateway binary")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ActorUpstream)
    upstream.arguments = []
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    port = free_port()
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
            "name": "actor",
            "mcp": {"host": f"http://127.0.0.1:{upstream.server_port}/mcp"},
            "policies": {"backendAuth": {"key": {"value": "local-test-token"}}},
        }
    ]
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(config))
    process = None
    try:
        with (tmp_path / "gateway.log").open("w") as log:
            process = subprocess.Popen([str(Path(binary).resolve()), "-f", str(path)], stdout=log, stderr=log)  # noqa: S603
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
            ) as client:
                for _ in range(100):
                    assert process.poll() is None, (tmp_path / "gateway.log").read_text()
                    try:
                        if (await client.get("/api/runtime")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Gateway did not start")
                headers = {
                    "accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-06-18",
                }
                initialized = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "actor-test", "version": "1"},
                        },
                    },
                )
                initialized.raise_for_status()
                if session := initialized.headers.get("mcp-session-id"):
                    headers["mcp-session-id"] = session
                await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                )
                response = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "actor_call",
                            "arguments": {"name": "safe", "__denkzeug_actor": "opaque-test-capability"},
                        },
                    },
                )
                response.raise_for_status()
                assert _rpc_result(response).get("content"), response.text
                assert upstream.arguments == [{"name": "safe", "__denkzeug_actor": "opaque-test-capability"}]
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=3)
