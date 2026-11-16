import json
from unittest.mock import patch

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
    monkeypatch.setenv("MCP_APPS_SANDBOX_URL", "javascript:alert(1)")
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
