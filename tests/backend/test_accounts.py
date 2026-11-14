"""Account lifecycle tests use real Argon2 hashes and real persisted sessions."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from nautionette_backend import auth, auth_mail, main
from nautionette_backend.auth_store import digest, issue_token
from nautionette_backend.config import settings

PASSWORD = "a correct horse battery staple for tests"
NEW_PASSWORD = "a different long private passphrase 728!"


def web_client():
    return TestClient(main.app, base_url="https://testserver")


def login(http, *, native=False):
    return http.post("/api/auth/login", json={"username": "admin", "password": PASSWORD, "native": native})


def csrf(http):
    return {"X-CSRF-Token": http.cookies.get("dz_csrf", "")}


@pytest.fixture
def mail(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(settings, "smtp_from", "accounts@example.test")
    monkeypatch.setattr(settings, "public_url", "https://testserver")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    sent = []
    monkeypatch.setattr(auth_mail, "send", sent.append)
    return sent


def test_legacy_token_is_not_a_production_login_secret(anonymous, db):
    # The fixture has one explicitly seeded session; arbitrary APP_TOKEN values
    # and old query/header authentication mechanisms no longer authenticate.
    assert anonymous.get("/api/auth/me", params={"token": "test-app-token"}).status_code == 401
    assert anonymous.get("/api/auth/me", headers={"X-Auth-Token": "test-app-token"}).status_code == 401
    assert anonymous.get("/api/auth/me", headers={"Authorization": "Bearer another-token"}).status_code == 401


def test_browser_signin_cookie_csrf_and_logout(backend):
    http = web_client()
    result = login(http)
    assert result.status_code == 200
    assert "access_token" not in result.json()
    assert "httponly" in result.headers.get_list("set-cookie")[0].lower()
    assert "secure" in result.headers.get_list("set-cookie")[0].lower()
    assert http.get("/api/auth/me").json()["user"]["id"] == "test-admin"
    assert http.post("/api/auth/logout", json={}).status_code == 403
    assert http.post("/api/auth/logout", json={}, headers=csrf(http)).status_code == 200
    assert http.get("/api/auth/me").status_code == 401


def test_origin_and_validation_never_echo_secrets(backend):
    http = web_client()
    assert login(http).status_code == 200
    result = http.patch(
        "/api/auth/profile",
        json={"display_name": "Changed"},
        headers={**csrf(http), "Origin": "https://evil.example"},
    )
    assert result.status_code == 403
    result = http.post("/api/auth/login", json={"username": "a", "password": "sensitive", "role": "admin"})
    assert result.status_code == 422
    assert "sensitive" not in result.text
    assert result.headers["cache-control"] == "no-store"


def test_native_refresh_rotation_detects_replay_and_revokes_family(backend):
    http = web_client()
    result = login(http, native=True).json()
    assert "dz_session" not in http.cookies
    headers = {"Authorization": "Bearer " + result["access_token"]}
    assert http.get("/api/auth/me", headers=headers).status_code == 200
    refreshed = http.post("/api/auth/refresh", json={"refresh_token": result["refresh_token"]})
    assert refreshed.status_code == 200
    fresh = refreshed.json()
    assert http.get("/api/auth/me", headers=headers).status_code == 401
    assert http.post("/api/auth/refresh", json={"refresh_token": result["refresh_token"]}).status_code == 401
    assert (
        http.get("/api/auth/me", headers={"Authorization": "Bearer " + fresh["access_token"]}).status_code
        == 401
    )


def test_password_change_revokes_every_session(backend):
    first, second = web_client(), web_client()
    assert login(first).status_code == login(second).status_code == 200
    result = first.post(
        "/api/auth/change-password",
        headers=csrf(first),
        json={"current_password": PASSWORD, "password": NEW_PASSWORD},
    )
    assert result.status_code == 200
    assert first.get("/api/auth/me").status_code == second.get("/api/auth/me").status_code == 401
    assert login(first).status_code == 401
    assert (
        first.post("/api/auth/login", json={"username": "admin", "password": NEW_PASSWORD}).status_code == 200
    )


def test_invite_policy_email_verification_and_one_use(client, db, mail):
    http = web_client()
    assert client.post("/api/auth/invitations", json={"email": "new@example.test"}).status_code == 403
    assert client.patch("/api/auth/policy", json={"registration_mode": "invite_only"}).status_code == 200
    created = client.post("/api/auth/invitations", json={"email": "new@example.test"})
    assert created.status_code == 200
    code = created.json()["code"]
    assert code not in str(db.query("SELECT * FROM auth_tokens"))
    assert "#invite=" in created.json()["link"]
    assert client.patch("/api/auth/policy", json={"registration_mode": "paused"}).status_code == 200
    assert (
        http.post(
            "/api/auth/register", json={"code": code, "username": "new.person", "password": PASSWORD}
        ).status_code
        == 403
    )
    client.patch("/api/auth/policy", json={"registration_mode": "invite_only"})
    body = {"code": code, "username": "new.person", "password": PASSWORD}
    assert http.post("/api/auth/register", json=body).json() == {"verification_required": True}
    assert http.post("/api/auth/register", json=body).status_code == 400
    assert (
        http.post("/api/auth/login", json={"username": "new.person", "password": PASSWORD}).status_code == 401
    )
    user = db.one("SELECT * FROM auth_users WHERE username='new.person'")
    assert user["role"] == "member" and not user["email_verified"]
    # Minting a token here stands in for possession of the bound mailbox.
    with db.transaction() as conn:
        _, token = issue_token(conn, "verify_email", user_id=user["id"], address=user["email"])
    assert http.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    assert http.post("/api/auth/verify-email", json={"token": token}).status_code == 400
    assert (
        http.post("/api/auth/login", json={"username": "new.person", "password": PASSWORD}).status_code == 200
    )
    assert http.get("/api/auth/users").status_code == 403
    assert (
        http.patch("/api/auth/policy", headers=csrf(http), json={"registration_mode": "paused"}).status_code
        == 403
    )
    # Outbox payload cannot expose a verification secret in a database-only backup.
    outbox = db.one("SELECT * FROM auth_mail_outbox")
    assert "https://testserver" not in outbox["encrypted_message"]


def test_recovery_generic_response_single_use_and_session_revocation(client, db, mail):
    http = web_client()
    known = http.post("/api/auth/forgot-password", json={"email": "admin@example.test"})
    unknown = http.post("/api/auth/forgot-password", json={"email": "unknown@example.test"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    with db.transaction() as conn:
        _, token = issue_token(conn, "reset", user_id="test-admin", address="admin@example.test")
    assert http.get("/api/auth/reset-password", params={"token": token}).status_code in {404, 405}
    assert client.get("/api/auth/me").status_code == 200
    result = http.post("/api/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})
    assert result.status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert (
        http.post("/api/auth/reset-password", json={"token": token, "password": NEW_PASSWORD}).status_code
        == 400
    )


def test_last_admin_and_stale_reauthentication(client, db):
    assert client.patch("/api/auth/users/test-admin", json={"role": "member"}).status_code == 409
    assert client.patch("/api/auth/users/test-admin", json={"status": "suspended"}).status_code == 409
    db.execute("UPDATE auth_sessions SET reauth_at=0")
    assert client.patch("/api/auth/policy", json={"registration_mode": "invite_only"}).status_code == 403
    assert client.post("/api/auth/reauth", json={"password": PASSWORD}).status_code == 200
    assert client.patch("/api/auth/policy", json={"registration_mode": "invite_only"}).status_code == 200


def test_sessions_cannot_be_revoked_across_accounts(client, db):
    assert client.delete("/api/auth/sessions/not-my-session").status_code == 404
    assert client.get("/api/auth/sessions").json()["sessions"][0]["current"]
    db.execute("UPDATE auth_sessions SET last_seen_at=?", (time.time() - 86401,))
    assert client.get("/api/auth/me").status_code == 401


def test_bootstrap_claims_every_legacy_chat_once(db, monkeypatch):
    # Real migration, not merely a new-admin login test.
    db.execute("DELETE FROM auth_users")
    chat = db.create_chat("Legacy private conversation", "default")
    monkeypatch.setattr(settings, "public_url", "https://testserver")
    token = auth.issue_setup_token()
    http = web_client()
    body = {"token": token, "username": "owner", "email": "owner@example.test", "password": PASSWORD}
    result = http.post("/api/auth/bootstrap", json=body)
    assert result.status_code == 200, result.text
    owner = result.json()["user"]["id"]
    assert db.one("SELECT owner_user_id FROM chats WHERE id=?", (chat["id"],))["owner_user_id"] == owner
    assert http.post("/api/auth/bootstrap", json=body).status_code in {400, 409}
    assert db.one("SELECT COUNT(*) AS n FROM auth_users")["n"] == 1
    assert digest(token) in str(db.query("SELECT * FROM auth_tokens"))
    assert token not in str(db.query("SELECT * FROM auth_tokens"))


async def test_mail_retry_redaction_and_cleanup(client, db, mail, monkeypatch):
    def failure(_):
        raise OSError("provider text might contain a secret")

    monkeypatch.setattr(auth_mail, "send", failure)
    client.post("/api/auth/test-email")
    await auth_mail.deliver_once()
    row = db.one("SELECT * FROM auth_mail_outbox")
    assert row["state"] == "queued" and row["attempts"] == 1
    assert row["error"] == "delivery_failed"
    db.execute("UPDATE auth_mail_outbox SET expires_at=0")
    await auth_mail.deliver_once()
    assert db.one("SELECT encrypted_message FROM auth_mail_outbox")["encrypted_message"] == ""
