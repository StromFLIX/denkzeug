"""Staging-only app registry + workflow delivery + isolated public CSP smoke."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlencode, urlsplit

from scripts.smoke_api import API, APIError

HTML = "<!doctype html><html><body><p>Deployment app smoke</p></body></html>"
APP_ID = "deployment-smoke"


def check(api):
    if api.call("/api/system").get("environment") != "staging":
        raise RuntimeError("MCP Apps smoke refuses non-staging environments")
    configured = api.call("/api/mcp-apps/runtime").get("sandbox_url", "")
    parent = urlsplit(api.url)
    sandbox = urlsplit(configured)
    if (
        sandbox.scheme != "https"
        or not sandbox.hostname
        or sandbox.username
        or sandbox.password
        or sandbox.netloc == parent.netloc
        or sandbox.path not in ("", "/")
        or sandbox.query
        or sandbox.fragment
    ):
        raise RuntimeError("Staging MCP_APPS_SANDBOX_URL must configure a separate HTTPS origin")
    # Deliberately do NOT use the authenticated API session for sandbox traffic.
    url = configured.rstrip("/") + "/sandbox.html?" + urlencode({"parent": api.url, "csp": "{}"})
    opener = urllib.request.build_opener()
    with opener.open(url, timeout=30) as response:  # noqa: S310 - validated HTTPS operator config
        if response.url != url:
            raise RuntimeError("Sandbox proxy unexpectedly redirected")
        csp = response.headers.get("Content-Security-Policy", "")
        if "connect-src 'none'" not in csp or "frame-ancestors " + api.url not in csp:
            raise RuntimeError("Sandbox response lacks the required HTTP CSP")
        if response.headers.get("X-DNS-Prefetch-Control") != "off":
            raise RuntimeError("Sandbox DNS-prefetch guard is missing")
        if "ui/notifications/sandbox-proxy-ready" not in response.read(32768).decode():
            raise RuntimeError("Sandbox origin does not serve the expected proxy")
    try:
        existing = api.call("/api/apps/" + APP_ID)
    except APIError as error:
        if error.status != 404:
            raise
        existing = None
    if existing and existing["app"]["archived"]:
        api.call(
            "/api/apps/" + APP_ID,
            "PATCH",
            {
                "archived": False,
                "expected_revision": existing["app"]["active_revision"],
            },
        )
    if not existing or existing["revision"]["html"] != HTML:
        existing = api.call(
            "/api/apps",
            "POST",
            {
                "id": APP_ID,
                "title": "Deployment smoke",
                "description": "Staging smoke account only",
                "html": HTML,
                "input_schema": {"type": "object"},
                "sample_data": {"count": 1},
                "change_note": "Deployment verification",
                "expected_revision": existing["app"]["active_revision"] if existing else 0,
            },
        )
    revision = existing["app"]["active_revision"]
    name = "mcp_apps_smoke_" + uuid.uuid4().hex
    path, chat_id = "/api/workflows/" + name, None
    code = """from temporalio import workflow
MANIFEST = {"schema":1,"name":"__NAME__","title":"Disposable MCP Apps smoke",
 "inputs":{"type":"object"},"outputs":{"type":"object"},"timeout_minutes":2}
@workflow.defn(name="__NAME__")
class AppSmoke:
 @workflow.run
 async def run(self, params: dict) -> dict:
  return {"summary":"App snapshot verified", "data":{"count":7},
          "ui":{"app":"deployment-smoke","revision":__REVISION__}}
""".replace("__NAME__", name).replace("__REVISION__", str(revision))
    created = False
    try:
        created = True
        if not api.call(path + "/deploy", "POST", {"code": code}).get("ready"):
            raise RuntimeError("App smoke workflow worker not ready")
        run_id = api.call(path + "/run", "POST", {"input": {}})["workflow_id"]
        for _ in range(60):
            info = api.call("/api/runs/" + run_id)
            status = info.get("run", {}).get("status")
            chat_id = api.call(path)["settings"].get("chat_id")
            if status in {"failed", "timed_out", "terminated", "canceled"}:
                raise RuntimeError("App smoke workflow failed")
            if status == "completed" and chat_id:
                messages = api.call("/api/chats/" + chat_id)["messages"]
                results = [
                    m for m in messages if m.get("meta", {}).get("run", {}).get("workflow_id") == run_id
                ]
                if len(results) == 1:
                    app = results[0].get("meta", {}).get("apps", [{}])[0]
                    instance = api.call(f"/api/chats/{chat_id}/apps/{app.get('id', 'missing')}")
                    if instance["html"] != HTML or instance["result"]["structuredContent"] != {"count": 7}:
                        raise RuntimeError("Saved app source/data mismatch")
                    if instance["revision"] != revision or results[0]["content"] != "App snapshot verified":
                        raise RuntimeError("App revision or readable fallback mismatch")
                    break
            time.sleep(2)
        else:
            raise RuntimeError("App result was not delivered")
        print(
            json.dumps(
                {
                    "mcp_apps_smoke": "passed",
                    "registry": "verified",
                    "workflow_snapshot": "verified",
                    "sandbox_http_csp": "verified",
                }
            )
        )
    finally:
        if created:
            if not chat_id:
                chat_id = api.call(path)["settings"].get("chat_id")
            if chat_id:
                api.call("/api/chats/" + chat_id, "DELETE")
            api.call(path, "DELETE")
        api.call("/api/apps/" + APP_ID, "PATCH", {"archived": True, "expected_revision": revision})


if __name__ == "__main__":
    with API() as api:
        check(api)
