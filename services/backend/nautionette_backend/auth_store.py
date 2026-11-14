"""Account schema and small cryptographic primitives; no service imports at import time.

Account data lives in the same transactional database as resource ownership. Raw
session, invitation and recovery secrets are never persisted here.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException

PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
# Bound memory under parallel sign-in/registration attacks (2 x 64 MiB).
_PASSWORD_SLOTS = threading.BoundedSemaphore(2)
# Equal-cost verification for unknown usernames. This is not an account password.
_DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
_COMMON_PASSWORDS = frozenset(
    normalized
    for value in (Path(__file__).with_name("data") / "common-passwords.txt").read_text().splitlines()
    if (normalized := re.sub(r"[^a-z0-9]", "", value.casefold()))
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 email TEXT UNIQUE COLLATE NOCASE, display_name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','member')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','pending')),
 email_verified INTEGER NOT NULL DEFAULT 0, must_change_password INTEGER NOT NULL DEFAULT 0,
 password_hash TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_workspaces (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_memberships (
 workspace_id TEXT NOT NULL REFERENCES auth_workspaces(id),
 user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('admin','member')), PRIMARY KEY(workspace_id,user_id)
);
CREATE TABLE IF NOT EXISTS auth_identities (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
 provider TEXT NOT NULL, issuer TEXT NOT NULL, subject TEXT NOT NULL,
 created_at REAL NOT NULL, UNIQUE(provider,issuer,subject)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE, csrf_hash TEXT NOT NULL,
 refresh_hash TEXT UNIQUE, native INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, last_seen_at REAL NOT NULL, expires_at REAL NOT NULL,
 access_expires_at REAL NOT NULL, reauth_at REAL NOT NULL, revoked_at REAL,
 device TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
CREATE TABLE IF NOT EXISTS auth_consumed_refresh (
 token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES auth_sessions(id) ON DELETE CASCADE,
 expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_tokens (
 id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
 purpose TEXT NOT NULL CHECK(purpose IN ('setup','invite','reset','verify_email','change_email')),
 user_id TEXT REFERENCES auth_users(id) ON DELETE CASCADE,
 email TEXT, role TEXT CHECK(role IN ('admin','member')), label TEXT NOT NULL DEFAULT '', created_by TEXT,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, consumed_at REAL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS auth_policy (
 id INTEGER PRIMARY KEY CHECK(id=1), registration_mode TEXT NOT NULL DEFAULT 'paused'
 CHECK(registration_mode IN ('paused','invite_only'))
);
INSERT OR IGNORE INTO auth_policy(id,registration_mode) VALUES (1,'paused');
CREATE TABLE IF NOT EXISTS auth_audit_events (
 id TEXT PRIMARY KEY, actor_user_id TEXT, subject_user_id TEXT, action TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_audit_age ON auth_audit_events(created_at);
CREATE TABLE IF NOT EXISTS auth_rate_limits (
 key TEXT PRIMARY KEY, started_at REAL NOT NULL, attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_mail_outbox (
 id TEXT PRIMARY KEY, recipient TEXT NOT NULL, encrypted_message TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT ''
);
"""


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    columns = conn.execute("PRAGMA table_info(auth_users)").fetchall()
    if any(row[1] == "email" and row[3] for row in columns):
        # Preserve accounts and every referencing session/membership when upgrading
        # the initial account schema. NULL permits many email-less accounts while
        # retaining uniqueness for addresses that are supplied.
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                create = _SCHEMA.split("CREATE TABLE IF NOT EXISTS auth_workspaces", 1)[0].strip()
                conn.execute(create.replace("IF NOT EXISTS auth_users", "auth_users_optional"))
                conn.execute("INSERT INTO auth_users_optional SELECT * FROM auth_users")
                conn.execute("DROP TABLE auth_users")
                conn.execute("ALTER TABLE auth_users_optional RENAME TO auth_users")
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Account email migration failed foreign-key validation")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    if not any(row[1] == "label" for row in conn.execute("PRAGMA table_info(auth_tokens)")):
        conn.execute("ALTER TABLE auth_tokens ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE auth_users SET status='active' WHERE status='pending'")
    conn.commit()


def store():
    from .db import db

    return db


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secret() -> str:
    return secrets.token_urlsafe(32)


def identifier() -> str:
    return uuid.uuid4().hex


def fail(status: int, code: str, message: str) -> None:
    raise HTTPException(status, detail={"code": code, "message": message})


def username(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", value):
        fail(422, "invalid_username", "Use 3–64 letters, numbers, dots, underscores or hyphens.")
    return value


def email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(
        r"[^\s@<>\x00-\x1f]+@[^\s@<>\x00-\x1f]+\.[^\s@<>\x00-\x1f]+", value
    ):
        fail(422, "invalid_email", "Enter a valid email address.")
    return value


def password_hash(password: str) -> str:
    if not 15 <= len(password) <= 1024 or len(password.encode()) > 4096:
        fail(422, "invalid_password", "Use a password between 15 and 1024 characters.")
    # Local-only checks: credentials are never sent to a breach-checking service.
    normalized = re.sub(r"[^a-z0-9]", "", password.lower())
    common = {"password", "password123", "password123456789", "123456789012345", "qwertyuiopasdfgh"}
    if (
        len(set(password)) < 5
        or normalized in common
        or normalized in _COMMON_PASSWORDS
        or any(password == password[:n] * (len(password) // n) for n in range(1, 5) if len(password) % n == 0)
    ):
        fail(422, "weak_password", "Choose a less predictable password or a long passphrase.")
    return rehash_verified_password(password)


def rehash_verified_password(password: str) -> str:
    """Only for an already verified credential; new credentials use password_hash."""
    with _PASSWORD_SLOTS:
        return PASSWORDS.hash(password)


def verify_password(encoded: str | None, password: str) -> bool:
    # Bound attacker-controlled Argon2 input before expensive processing.
    if len(password) > 1024 or len(password.encode()) > 4096:
        return False
    try:
        with _PASSWORD_SLOTS:
            valid = PASSWORDS.verify(encoded or _DUMMY_HASH, password)
        return bool(encoded) and valid
    except (VerificationError, InvalidHashError):
        return False


def public_user(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    return {
        key: (bool(row[key]) if key in {"email_verified", "must_change_password"} else row[key])
        for key in (
            "id",
            "username",
            "email",
            "display_name",
            "role",
            "status",
            "email_verified",
            "must_change_password",
            "created_at",
            "updated_at",
        )
    }


def audit(
    conn: sqlite3.Connection,
    action: str,
    actor: str | None = None,
    subject: str | None = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO auth_audit_events(id,actor_user_id,subject_user_id,action,detail,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (identifier(), actor, subject, action, detail, time.time()),
    )


def issue_token(
    conn: sqlite3.Connection,
    purpose: str,
    *,
    user_id: str | None = None,
    address: str | None = None,
    role: str | None = None,
    creator: str | None = None,
    ttl: int = 1800,
) -> tuple[str, str]:
    token, token_id, now = secret(), identifier(), time.time()
    conn.execute(
        "INSERT INTO auth_tokens(id,token_hash,purpose,user_id,email,role,created_by,created_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (token_id, digest(token), purpose, user_id, address, role, creator, now, now + ttl),
    )
    return token_id, token


def valid_token(conn: sqlite3.Connection, token: str, purpose: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM auth_tokens WHERE token_hash=? AND purpose=? AND expires_at>? "
        "AND consumed_at IS NULL AND revoked_at IS NULL",
        (digest(token.strip()), purpose, time.time()),
    ).fetchone()
    if not row:
        fail(400, "invalid_link", "This link or code has expired, was revoked, or has already been used.")
    return row


def throttle(key: str, limit: int, window: int = 300) -> None:
    now = time.time()
    with store().transaction() as conn:
        hashed = digest(key)
        row = conn.execute("SELECT * FROM auth_rate_limits WHERE key=?", (hashed,)).fetchone()
        if row and row["started_at"] + window > now:
            if row["attempts"] >= limit:
                raise HTTPException(
                    429,
                    detail={"code": "rate_limited", "message": "Please try again later."},
                    headers={"Retry-After": str(max(1, int(row["started_at"] + window - now)))},
                )
            conn.execute("UPDATE auth_rate_limits SET attempts=attempts+1 WHERE key=?", (hashed,))
        else:
            conn.execute("INSERT OR REPLACE INTO auth_rate_limits VALUES(?,?,1)", (hashed, now))
