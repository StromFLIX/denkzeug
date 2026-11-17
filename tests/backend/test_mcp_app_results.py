import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from nautionette_backend import app_sandbox, mcp_app_results
from nautionette_backend.agent.runner import Timeline
from nautionette_backend.routers.frontend import app_sandbox_url


def live_chat(db):
    chat = db.create_chat("App test", "default", "openai/test")
    db.execute(
        "INSERT INTO messages(id,chat_id,role,content,created_at) VALUES('input',?,'user','test',1)",
        (chat["id"],),
    )
    db.execute(
        "INSERT INTO chat_turns(id,chat_id,user_id,state) VALUES('turn',?,'input','running')", (chat["id"],)
    )
    return {"chat_id": chat["id"], "turn_id": "turn"}


def event():
    return {
        "type": "tool_done",
        "id": "tool-1",
        "name": "weather_show",
        "result": "Weather",
        "mcp_app": {
            "title": "Weather",
            "resource_uri": "ui://weather+forecast/view",
            "html": "<html><body>Forecast</body></html>",
            "input": {"city": "Bern"},
            "result": {
                "content": [{"type": "text", "text": "Weather"}],
                "structuredContent": {"degrees": 12},
                "_meta": {"private": "ui only"},
            },
            "csp": {"resourceDomains": ["https://cdn.example"], "connectDomains": []},
        },
    }


def test_capture_is_private_idempotent_and_drops_raw_html(db, client):
    job = live_chat(db)
    captured = mcp_app_results.capture_result(job, event())
    assert "mcp_app" not in captured
    assert "html" not in json.dumps(captured)
    assert "app" in captured
    assert mcp_app_results.capture_result(job, event()) == captured
    assert len(db.query("SELECT id FROM mcp_app_instances")) == 1
    response = client.get(f"/api/chats/{job['chat_id']}/apps/{captured['app']['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["input"] == {"city": "Bern"}
    assert body["result"]["_meta"] == {"private": "ui only"}
    assert body["source"]["resource_uri"] == "ui://weather+forecast/view"
    assert client.get(f"/api/chats/not-owned/apps/{captured['app']['id']}").status_code == 404
    timeline = Timeline()
    timeline.finish_tool(captured)
    assert timeline.steps[0]["app"] == captured["app"]
    db.execute("UPDATE chat_turns SET state='completed' WHERE id='turn'")
    assert client.get(f"/api/chats/{job['chat_id']}/apps/{captured['app']['id']}").status_code == 200
    db.execute("DELETE FROM chats WHERE id=?", (job["chat_id"],))
    assert db.query("SELECT * FROM mcp_app_instances") == []


@pytest.mark.parametrize(
    "change",
    [
        {"resource_uri": "https://evil.example"},
        {"html": "x" * 1048577},
        {"csp": {"connectDomains": ["https://example.com; script-src *"]}},
        {"csp": {"connectDomains": ["file:///etc/passwd"]}},
        {"input": []},
        {"result": []},
    ],
)
def test_bad_view_never_fails_or_replays_business_tool(db, change):
    job = live_chat(db)
    original = event()
    original["mcp_app"].update(change)
    captured = mcp_app_results.capture_result(job, original)
    assert captured["result"] == "Weather"
    assert captured["app_error"]
    assert "mcp_app" not in captured and "app" not in captured
    assert db.query("SELECT id FROM mcp_app_instances") == []


def test_expired_turn_cannot_create_view(db):
    job = live_chat(db)
    db.execute("UPDATE chat_turns SET state='completed' WHERE id='turn'")
    assert "app" not in mcp_app_results.capture_result(job, event())
    assert "mcp_app" not in mcp_app_results.capture_result({}, event())


def test_runtime_requires_auth_and_separate_origin(client, monkeypatch):
    monkeypatch.setenv("MCP_APPS_SANDBOX_URL", "https://sandbox.example")
    assert client.get("/api/mcp-apps/runtime").json()["sandbox_url"] == "https://sandbox.example"
    for value in ("javascript:alert(1)", "capacitor://localhost", "capacitor://localhost/", "null"):
        monkeypatch.setenv("MCP_APPS_SANDBOX_URL", value)
        assert app_sandbox_url() == ""
    with patch("nautionette_backend.routers.frontend.settings.public_url", "https://app.example"):
        monkeypatch.setenv("MCP_APPS_SANDBOX_URL", "https://app.example")
        assert app_sandbox_url() == ""


def test_sandbox_csp_is_http_enforced_and_origin_allowlisted(monkeypatch):
    monkeypatch.setenv("MCP_APPS_HOST_ORIGINS", "https://app.example")
    headers = app_sandbox.sandbox_policy("https://app.example", {"resourceDomains": ["https://cdn.example"]})
    assert "connect-src 'none'" in headers["Content-Security-Policy"]
    assert "script-src 'unsafe-inline' https://cdn.example" in headers["Content-Security-Policy"]
    assert "unsafe-eval" not in headers["Content-Security-Policy"]
    for value in [
        "https://evil.example; script-src *",
        "file:///etc/passwd",
        "https://a.example/path",
        "https://user:pass@a.example",
        "https://*.example",
    ]:
        with pytest.raises(ValueError):
            app_sandbox.sandbox_policy("https://app.example", {"resourceDomains": [value]})
    with pytest.raises(ValueError):
        app_sandbox.sandbox_policy("https://evil.example", {})


@pytest.mark.parametrize("value", ["capacitor://localhost", "capacitor://localhost/"])
def test_native_origin_requires_explicit_app_host_context(value):
    assert app_sandbox.origin(value, app_host=True) == "capacitor://localhost"
    for local in (True, False):
        with pytest.raises(ValueError):
            app_sandbox.origin(value, local=local)


@pytest.mark.parametrize(
    "value",
    [
        "null",
        "file:///",
        "data:text/html,test",
        "javascript:alert(1)",
        "custom://localhost",
        "capacitor://evil.example",
        "capacitor://localhost.evil.example",
        "capacitor://127.0.0.1",
        "capacitor://user@localhost",
        "capacitor://:pass@localhost",
        "capacitor://localhost:443",
        "capacitor://localhost:",
        "capacitor://localhost/path",
        "capacitor://localhost//",
        "capacitor://localhost?",
        "capacitor://localhost#",
        "capacitor://localhost?x=1",
        "capacitor://localhost#fragment",
        "capacitor://localhost/;script-src *",
        "capacitor://localhost\n",
    ],
)
def test_app_host_origin_rejects_opaque_and_non_bundled_origins(value):
    with pytest.raises(ValueError):
        app_sandbox.origin(value, app_host=True)


@pytest.mark.parametrize("setting", [None, "true", "false", " FALSE "])
def test_native_allowlist_union_and_opt_out(monkeypatch, setting):
    monkeypatch.setenv("MCP_APPS_HOST_ORIGINS", "https://app.example, http://127.0.0.1:8080")
    monkeypatch.delenv("MCP_APPS_INCLUDE_NATIVE_ORIGINS", raising=False)
    if setting is not None:
        monkeypatch.setenv("MCP_APPS_INCLUDE_NATIVE_ORIGINS", setting)
    expected = {"https://app.example", "http://127.0.0.1:8080"}
    if setting is None or setting == "true":
        expected.update({"capacitor://localhost", "https://localhost"})
    assert app_sandbox.approved_origins() == expected


def test_native_allowlist_defaults_and_explicit_configuration(monkeypatch):
    monkeypatch.delenv("MCP_APPS_HOST_ORIGINS", raising=False)
    monkeypatch.delenv("DENKZEUG_PUBLIC_URL", raising=False)
    monkeypatch.delenv("MCP_APPS_INCLUDE_NATIVE_ORIGINS", raising=False)
    assert app_sandbox.approved_origins() == {"capacitor://localhost", "https://localhost"}
    monkeypatch.setenv("DENKZEUG_PUBLIC_URL", "https://app.example")
    assert "https://app.example" in app_sandbox.approved_origins()
    monkeypatch.setenv("MCP_APPS_INCLUDE_NATIVE_ORIGINS", "false")
    assert app_sandbox.approved_origins() == {"https://app.example"}
    monkeypatch.setenv("MCP_APPS_HOST_ORIGINS", "capacitor://localhost/,https://localhost")
    assert app_sandbox.approved_origins() == {"capacitor://localhost", "https://localhost"}


@pytest.mark.parametrize("value", ["null", "capacitor://evil.example", "custom://localhost"])
def test_invalid_configured_app_host_is_not_approved(monkeypatch, value):
    monkeypatch.setenv("MCP_APPS_HOST_ORIGINS", value)
    with pytest.raises(ValueError):
        app_sandbox.approved_origins()


@pytest.fixture
def sandbox_request(monkeypatch):
    monkeypatch.setenv("MCP_APPS_HOST_ORIGINS", "https://app.example,http://127.0.0.1:8080")
    monkeypatch.delenv("MCP_APPS_INCLUDE_NATIVE_ORIGINS", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app_sandbox.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(parent, csp=None, *, host="sandbox.example"):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            query = urlencode({"parent": parent, "csp": json.dumps(csp or {})})
            connection.request("GET", f"/sandbox.html?{query}", headers={"Host": host})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    try:
        yield request
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize(
    ("parent", "canonical"),
    [
        ("capacitor://localhost", "capacitor://localhost"),
        ("capacitor://localhost/", "capacitor://localhost"),
        ("https://localhost", "https://localhost"),
        ("https://app.example", "https://app.example"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ],
)
def test_sandbox_handler_allows_native_and_configured_web_parents(sandbox_request, parent, canonical):
    status, headers, body = sandbox_request(parent)
    assert status == 200
    assert body == app_sandbox.DOCUMENT
    policy = headers["Content-Security-Policy"]
    assert policy.endswith(f"frame-ancestors {canonical}")
    for directive in (
        "default-src 'none'",
        "connect-src 'none'",
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ):
        assert directive in policy
    assert "unsafe-eval" not in policy
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize(
    "parent",
    [
        "null",
        "file:///",
        "data:text/html,test",
        "javascript:alert(1)",
        "custom://localhost",
        "capacitor://evil.example",
        "capacitor://user@localhost",
        "capacitor://localhost:443",
        "capacitor://localhost/path",
        "capacitor://localhost?",
        "capacitor://localhost#",
        "https://evil.example",
        "http://remote.example",
    ],
)
def test_sandbox_handler_rejects_unapproved_parents(sandbox_request, parent):
    assert sandbox_request(parent)[0] == 400


@pytest.mark.parametrize("parent", ["capacitor://localhost", "https://localhost"])
def test_sandbox_handler_native_opt_out_and_explicit_override(sandbox_request, monkeypatch, parent):
    monkeypatch.setenv("MCP_APPS_INCLUDE_NATIVE_ORIGINS", "false")
    assert sandbox_request(parent)[0] == 400
    monkeypatch.setenv("MCP_APPS_HOST_ORIGINS", parent)
    assert sandbox_request(parent)[0] == 200


@pytest.mark.parametrize("key", ["resourceDomains", "connectDomains", "frameDomains", "baseUriDomains"])
@pytest.mark.parametrize("value", ["capacitor://localhost", "capacitor://evil.example", "null", "file:///"])
def test_sandbox_handler_never_allows_native_or_opaque_csp_domains(sandbox_request, key, value):
    assert sandbox_request("capacitor://localhost", {key: [value]})[0] == 400


def test_sandbox_handler_preserves_separate_origin_guard(sandbox_request):
    assert sandbox_request("https://app.example", host="app.example")[0] == 400
    assert sandbox_request("capacitor://localhost", host="localhost")[0] == 400
