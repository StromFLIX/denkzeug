"""Permanent integration credentials preserve all scope, ownership and revocation gates."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from nautionette_backend import api_keys, main
from nautionette_backend.account_streams import live
from nautionette_backend.config import settings
from nautionette_backend.security import Principal

from .test_api_keys import create, external


def test_explicit_null_is_permanent_but_omission_keeps_the_default(client, db, monkeypatch):
    default = create(client)
    assert 89 * 86400 < default["token"]["expires_at"] - time.time() <= 90 * 86400
    permanent = create(client, expires_at=None)
    token_id = permanent["token"]["id"]
    assert permanent["token"]["expires_at"] is None
    assert db.one("SELECT expires_at FROM api_keys WHERE id=?", (token_id,))["expires_at"] is None
    actor = Principal("test-admin", "admin", token_id=token_id)
    now = time.time()
    with monkeypatch.context() as context:
        context.setattr(api_keys.time, "time", lambda: now + 20 * 365 * 86400)
        assert external(permanent["secret"]).get("/api/system").status_code == 200
        assert live(actor)
    rotated = client.post(f"/api/tokens/{token_id}/rotate")
    assert rotated.status_code == 201, rotated.text
    replacement = rotated.json()
    assert replacement["token"]["expires_at"] is None
    assert external(permanent["secret"]).get("/api/system").status_code == 401
    assert external(replacement["secret"]).get("/api/system").status_code == 200
    assert external(replacement["secret"]).get("/api/settings").status_code == 403
    assert client.delete(f"/api/tokens/{replacement['token']['id']}").status_code == 200
    assert external(replacement["secret"]).get("/api/system").status_code == 401
    assert not live(actor)


def test_permanent_keys_still_obey_account_suspension(client, db):
    permanent = create(client, expires_at=None)
    db.execute("UPDATE auth_users SET status='suspended' WHERE id='test-admin'")
    assert external(permanent["secret"]).get("/api/system").status_code == 401


def test_permanent_workflow_token_supports_get_post_and_typed_inputs(client, db, backend, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://testserver")
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "text": {"type": "string"},
            "items": {"type": "array"},
        },
    }
    backend.authoring.workflows["integration"] = {
        "name": "integration",
        "code": "",
        "manifest": {"inputs": schema},
    }
    backend.authoring.workflows["other"] = {"name": "other", "code": "", "manifest": {}}
    response = client.post("/api/workflows/integration/webhook-token", json={"days": None})
    assert response.status_code == 201, response.text
    credential = response.json()
    assert credential["expires_at"] is None
    assert credential["token"]["grants"] == [
        {"scope": "workflows:trigger", "resources": [api_keys.workflow_identity("integration")]}
    ]
    http = TestClient(main.app)
    expected = {"count": 3, "enabled": True, "text": "123", "items": ["a", None]}
    get = http.get(credential["url"], params={"token": credential["secret"], "input": json.dumps(expected)})
    assert get.status_code == 200, get.text
    stored = db.one("SELECT input FROM runs WHERE workflow_id=?", (get.json()["workflow_id"],))
    assert json.loads(stored["input"]) == expected
    post = http.post(credential["url"], json=expected)
    assert post.status_code == 200, post.text
    legacy = http.get(
        credential["url"],
        params={"token": credential["secret"], "count": "3", "enabled": "true", "text": "123"},
    )
    assert legacy.status_code == 200, legacy.text
    stored = db.one("SELECT input FROM runs WHERE workflow_id=?", (legacy.json()["workflow_id"],))
    assert json.loads(stored["input"]) == {"count": 3, "enabled": True, "text": "123"}
    restricted = external(credential["secret"])
    assert restricted.get("/api/triggers/other").status_code == 403
    assert restricted.get("/api/workflows/integration").status_code == 403
    assert restricted.post("/api/workflows/integration/run", json={}).status_code == 403
    assert client.get("/api/triggers/integration").status_code == 403  # Native session is not a trigger key.
    assert http.head(credential["url"]).status_code in {404, 405}
    for payload in ["not-json", "[]", "null"]:
        assert (
            http.get(credential["url"], params={"token": credential["secret"], "input": payload}).status_code
            == 422
        )
    assert (
        http.get(
            credential["url"], params={"token": credential["secret"], "input": "{}", "count": "1"}
        ).status_code
        == 422
    )
    assert (
        http.get(credential["url"], params={"token": credential["secret"], "input": "x" * 17000}).status_code
        == 413
    )
    rows = client.get(f"/api/tokens/{credential['token']['id']}/requests").json()["requests"]
    assert any(row["method"] == "GET" and row["run_id"] == get.json()["workflow_id"] for row in rows)
    assert credential["secret"] not in json.dumps(rows)
    db.forget_workflow("integration")
    api_keys.workflow_identity("integration")
    assert http.get(credential["url"]).status_code == 403
    rotated = client.post(f"/api/tokens/{credential['token']['id']}/rotate")
    assert rotated.status_code == 404  # Deleted generation cannot be re-issued.


def test_cookie_only_get_never_starts_a_workflow(backend):
    http = TestClient(main.app, base_url="https://testserver")
    response = http.post(
        "/api/auth/login", json={"username": "admin", "password": "a correct horse battery staple for tests"}
    )
    assert response.status_code == 200
    assert http.get("/api/triggers/anything").status_code == 403
    assert not backend.db.query("SELECT id FROM runs")


def test_permanent_webhook_rotation_and_revocation(client, backend, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://testserver")
    backend.authoring.workflows["rotate"] = {"name": "rotate", "code": "", "manifest": {}}
    first = client.post("/api/workflows/rotate/webhook-token", json={"days": None}).json()
    second = client.post(f"/api/tokens/{first['token']['id']}/rotate").json()
    assert second["token"]["expires_at"] is None
    http = TestClient(main.app)
    assert http.get(first["url"]).status_code == 401
    assert http.get(second["url"]).status_code == 200
    assert client.delete(f"/api/tokens/{second['token']['id']}").status_code == 200
    assert http.post(second["url"], json={}).status_code == 401


def test_nullable_expiry_migration_preserves_hashes_history_and_foreign_keys():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE auth_users(id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO auth_users VALUES('owner')")
    conn.executescript(api_keys.SCHEMA.replace("expires_at REAL,", "expires_at REAL NOT NULL,"))
    conn.execute(
        "INSERT INTO api_keys(id,owner_user_id,name,prefix,secret_hash,kind,grants,created_at,expires_at) "
        "VALUES('legacy','owner','Existing','prefix','hash','api_key','[]',1,9999999999)"
    )
    conn.execute(
        "INSERT INTO api_key_requests(token_id,request_id,created_at,method,route) "
        "VALUES('legacy','trace',2,'POST','/api/triggers/example')"
    )
    conn.execute("INSERT INTO api_key_daily VALUES('legacy',0,1,0)")
    conn.commit()
    api_keys.install(conn)
    assert conn.execute("SELECT secret_hash,expires_at FROM api_keys").fetchone() == ("hash", 9999999999)
    assert conn.execute("SELECT request_id FROM api_key_requests").fetchone() == ("trace",)
    assert conn.execute("SELECT requests FROM api_key_daily").fetchone() == (1,)
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.execute("UPDATE api_keys SET expires_at=NULL")
    conn.commit()
    api_keys.install(conn)  # Idempotent, including already non-expiring keys.
    assert conn.execute("SELECT expires_at FROM api_keys").fetchone() == (None,)
    conn.execute("DELETE FROM api_keys WHERE id='legacy'")
    assert not conn.execute("SELECT * FROM api_key_requests").fetchall()
    assert not conn.execute("SELECT * FROM api_key_daily").fetchall()
    conn.close()


@pytest.mark.parametrize("days", [0, -1, False, "never"])
def test_never_expire_is_explicit_null_not_an_invalid_duration(client, days):
    response = client.post("/api/workflows/missing/webhook-token", json={"days": days})
    assert response.status_code == 422
