"""SSO readiness: immutable issuer/subject identities, never email-based linking."""

import pytest
from fastapi import HTTPException
from nautionette_backend.auth_identities import (
    VerifiedIdentity,
    link_identity,
    resolve_identity,
    unlink_identity,
)
from nautionette_backend.security import Principal


def test_provider_identity_must_be_explicitly_linked(db):
    identity = VerifiedIdentity("example", "https://identity.example.test", "123", "admin@example.test", True)
    with pytest.raises(HTTPException) as error:
        resolve_identity(identity)
    assert error.value.status_code == 403  # A matching verified email is not an account credential.
    principal = Principal("test-admin", "admin", "test-session")
    link_identity(principal, identity)
    assert resolve_identity(identity)["id"] == "test-admin"
    with pytest.raises(HTTPException):
        resolve_identity(VerifiedIdentity("example", "https://other.example.test", "123"))
    linked = db.one("SELECT id FROM auth_identities")
    db.execute("UPDATE auth_users SET password_hash=NULL WHERE id='test-admin'")
    with pytest.raises(HTTPException) as error:
        unlink_identity(principal, linked["id"])
    assert error.value.status_code == 409
    db.execute("UPDATE auth_users SET status='suspended' WHERE id='test-admin'")
    with pytest.raises(HTTPException):
        resolve_identity(identity)


def test_identity_linking_requires_recent_human_authentication(db):
    identity = VerifiedIdentity("example", "https://identity.example.test", "123")
    with pytest.raises(HTTPException):
        link_identity(Principal("test-admin", "admin", None, service=True), identity)
    db.execute("UPDATE auth_sessions SET reauth_at=0")
    with pytest.raises(HTTPException):
        link_identity(Principal("test-admin", "admin", "test-session"), identity)


def test_invalid_provider_identity_fails_before_storage():
    with pytest.raises(ValueError):
        VerifiedIdentity("example", "http://identity.example.test", "123")
