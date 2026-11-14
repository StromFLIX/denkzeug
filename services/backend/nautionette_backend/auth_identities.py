"""Provider-independent identity boundary for future OpenID Connect adapters.

There is intentionally no public SSO route or configurable provider yet. A future
adapter must validate code/PKCE, state, nonce, issuer, signature and audience
before returning VerifiedIdentity. Provider claims never imply workspace access.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from .auth_store import audit, fail, identifier, public_user, store
from .security import Principal


@dataclass(frozen=True)
class VerifiedIdentity:
    provider: str
    issuer: str
    subject: str
    email: str | None = None
    email_verified: bool = False

    def __post_init__(self) -> None:
        issuer = urlsplit(self.issuer)
        if (
            not self.provider
            or len(self.provider) > 100
            or not self.subject
            or len(self.subject) > 512
            or issuer.scheme != "https"
            or not issuer.hostname
            or issuer.username
            or issuer.password
            or issuer.fragment
            or issuer.query
        ):
            raise ValueError("Invalid verified provider identity")


class IdentityProvider(Protocol):
    """Code exchange/validation stays in a maintained provider protocol library."""

    async def authorization_url(
        self, *, state: str, nonce: str, pkce_challenge: str, redirect_uri: str
    ) -> str: ...

    async def exchange(
        self, *, code: str, verifier: str, expected_nonce: str, redirect_uri: str
    ) -> VerifiedIdentity: ...


def resolve_identity(identity: VerifiedIdentity) -> dict:
    row = store().one(
        "SELECT u.* FROM auth_identities i JOIN auth_users u ON u.id=i.user_id "
        "WHERE i.provider=? AND i.issuer=? AND i.subject=?",
        (identity.provider, identity.issuer, identity.subject),
    )
    if not row or row["status"] != "active":
        # Never look up or link by email, even when a provider says it is verified.
        fail(403, "membership_required", "Link this identity from an existing invited account first.")
    return public_user(row)


def _recent(conn, principal: Principal) -> None:
    row = conn.execute(
        "SELECT s.reauth_at,s.revoked_at,s.expires_at,u.status FROM auth_sessions s "
        "JOIN auth_users u ON u.id=s.user_id WHERE s.id=? AND s.user_id=?",
        (principal.session_id, principal.user_id),
    ).fetchone()
    if (
        principal.service
        or not row
        or row["revoked_at"] is not None
        or row["status"] != "active"
        or row["expires_at"] <= time.time()
        or row["reauth_at"] < time.time() - 600
    ):
        fail(403, "reauthentication_required", "Confirm your existing sign-in method first.")


def link_identity(principal: Principal, identity: VerifiedIdentity) -> None:
    with store().transaction() as conn:
        _recent(conn, principal)
        existing = conn.execute(
            "SELECT user_id FROM auth_identities WHERE provider=? AND issuer=? AND subject=?",
            (identity.provider, identity.issuer, identity.subject),
        ).fetchone()
        if existing:
            if existing["user_id"] != principal.user_id:
                fail(409, "identity_unavailable", "This identity is already linked to another account.")
            return
        conn.execute(
            "INSERT INTO auth_identities VALUES(?,?,?,?,?,?)",
            (
                identifier(),
                principal.user_id,
                identity.provider,
                identity.issuer,
                identity.subject,
                time.time(),
            ),
        )
        audit(conn, "identity.linked", principal.user_id, principal.user_id, identity.provider)


def unlink_identity(principal: Principal, identity_id: str) -> None:
    with store().transaction() as conn:
        _recent(conn, principal)
        user = conn.execute(
            "SELECT password_hash FROM auth_users WHERE id=?", (principal.user_id,)
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM auth_identities WHERE user_id=?", (principal.user_id,)
        ).fetchone()[0]
        if not user["password_hash"] and count <= 1:
            fail(
                409,
                "last_authentication_method",
                "Add another sign-in method before unlinking this identity.",
            )
        changed = conn.execute(
            "DELETE FROM auth_identities WHERE id=? AND user_id=?", (identity_id, principal.user_id)
        )
        if not changed.rowcount:
            fail(404, "not_found", "Linked identity not found.")
        audit(conn, "identity.unlinked", principal.user_id, principal.user_id)
