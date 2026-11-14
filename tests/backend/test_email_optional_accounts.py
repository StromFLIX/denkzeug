"""Email-free bootstrap, copy/paste invitations and administrator recovery."""

import sqlite3
import time

from fastapi.testclient import TestClient
from nautionette_backend import auth, auth_store, main
from nautionette_backend.config import settings
from nautionette_backend.security import current_principal

PASSWORD = "a private email-free account passphrase 728!"


def test_copy_paste_invites_and_reset_codes_need_no_email_or_smtp(client, db, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_from", "")
    monkeypatch.setattr(settings, "public_url", "")
    assert client.patch("/api/auth/policy", json={"registration_mode": "invite_only"}).status_code == 200
    first = client.post("/api/auth/invitations", json={"label": "For Sam"})
    second = client.post("/api/auth/invitations", json={})
    assert first.status_code == second.status_code == 200
    code = first.json()["code"]
    assert first.json()["link"] is None
    assert first.json()["invitation"]["label"] == "For Sam"
    assert first.json()["invitation"]["email"] is None
    person = TestClient(main.app)
    registered = person.post(
        "/api/auth/register",
        json={"code": f" {code}\n", "username": "sam", "password": PASSWORD, "native": True},
    )
    assert registered.status_code == 200, registered.text
    user = registered.json()["user"]
    assert user["email"] is None and not user["email_verified"] and user["status"] == "active"
    person.headers["Authorization"] = "Bearer " + registered.json()["access_token"]
    assert person.get("/api/auth/me").status_code == 200
    refreshed = person.post("/api/auth/refresh", json={"refresh_token": registered.json()["refresh_token"]})
    assert refreshed.status_code == 200
    person.headers["Authorization"] = "Bearer " + refreshed.json()["access_token"]
    other = TestClient(main.app).post(
        "/api/auth/register",
        json={"code": second.json()["code"], "username": "alex", "password": PASSWORD, "native": True},
    )
    assert other.status_code == 200  # NULL emails are not a singleton or shared identity.
    issued = client.post(f"/api/auth/users/{user['id']}/recovery", json={})
    assert issued.status_code == 200
    assert issued.json()["link"] is None
    reset_code = issued.json()["code"]
    assert reset_code not in str(db.query("SELECT * FROM auth_tokens"))
    new_password = "a new email-free recovery passphrase 456!"
    reset = TestClient(main.app).post(
        "/api/auth/reset-password", json={"token": reset_code, "password": new_password}
    )
    assert reset.status_code == 200, reset.text
    assert person.get("/api/auth/me").status_code == 401
    assert (
        person.post(
            "/api/auth/login", json={"username": "sam", "password": new_password, "native": True}
        ).status_code
        == 200
    )
    assert (
        person.post(
            "/api/auth/reset-password", json={"token": reset_code, "password": new_password}
        ).status_code
        == 400
    )
    assert not db.query("SELECT * FROM auth_mail_outbox")
    assert db.one(
        "SELECT action FROM auth_audit_events WHERE subject_user_id=? "
        "AND action='password.recovery_code_issued'",
        (user["id"],),
    )


def test_first_admin_can_omit_email_and_still_owns_legacy_data(db, monkeypatch):
    db.execute("DELETE FROM auth_users")
    db.execute("UPDATE access_state SET legacy_owner=NULL WHERE id=1")
    current_principal.set(None)
    legacy = db.create_chat("My existing private chat", "default")
    setup = auth.issue_setup_token()
    http = TestClient(main.app)
    result = http.post(
        "/api/auth/bootstrap",
        json={"token": setup, "username": "owner", "password": PASSWORD, "native": True},
    )
    assert result.status_code == 200, result.text
    assert result.json()["user"]["email"] is None
    owner = result.json()["user"]["id"]
    http.headers["Authorization"] = "Bearer " + result.json()["access_token"]
    assert http.get(f"/api/chats/{legacy['id']}").status_code == 200
    assert db.one("SELECT owner_user_id FROM chats WHERE id=?", (legacy["id"],))["owner_user_id"] == owner
    assert http.patch(f"/api/auth/users/{owner}", json={"role": "member"}).status_code == 409


def test_optional_recovery_email_can_be_added_and_removed_without_smtp(client, db, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    changed = client.patch(
        "/api/auth/profile",
        json={
            "email": "new-optional@example.test",
            "current_password": "a correct horse battery staple for tests",
        },
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["email"] == "new-optional@example.test"
    assert not changed.json()["user"]["email_verified"]
    assert client.get("/api/auth/me").status_code == 200
    removed = client.patch(
        "/api/auth/profile",
        json={"email": "", "current_password": "a correct horse battery staple for tests"},
    )
    assert removed.status_code == 200 and removed.json()["user"]["email"] is None
    assert client.get("/api/auth/me").status_code == 200


def test_optional_email_upgrade_preserves_account_and_referencing_session():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    old = auth_store._SCHEMA.replace(
        " email TEXT UNIQUE COLLATE NOCASE", " email TEXT NOT NULL UNIQUE COLLATE NOCASE"
    )
    conn.executescript(old)
    conn.execute(
        "INSERT INTO auth_users(id,username,email,display_name,role,created_at,updated_at) "
        "VALUES('owner','owner','owner@example.test','Owner','admin',0,0)"
    )
    now = time.time()
    conn.execute(
        "INSERT INTO auth_sessions(id,user_id,token_hash,csrf_hash,created_at,last_seen_at,expires_at,"
        "access_expires_at,reauth_at) VALUES('session','owner','hashed','csrf',?,?,?,?,?)",
        (now, now, now + 86400, now + 86400, now),
    )
    conn.commit()
    auth_store.install(conn)
    assert conn.execute("SELECT user_id FROM auth_sessions").fetchone()[0] == "owner"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    for name in ("alice", "bob"):
        conn.execute(
            "INSERT INTO auth_users(id,username,display_name,role,created_at,updated_at) "
            "VALUES(?,?,?,'member',0,0)",
            (name, name, name),
        )
    assert conn.execute("SELECT COUNT(*) FROM auth_users WHERE email IS NULL").fetchone()[0] == 2
    conn.close()
