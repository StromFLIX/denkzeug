"""Validated provider options and backend-only OAuth client authentication.

Never accept arbitrary token headers/body overrides. Secrets are sealed separately
from public reconnect settings, and are only opened at the token endpoint.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from urllib.parse import quote_plus

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from fastapi import HTTPException

from .config import settings
from .pi_package_secrets import seal, unseal

AUTH_METHODS = {"none", "client_secret_basic", "client_secret_post", "private_key_jwt", "client_secret_jwt"}
GRANTS = {"authorization_code", "device_code", "client_credentials"}
OPTIONS = {
    "grant_type",
    "token_endpoint_auth_method",
    "callback_mode",
    "metadata_mode",
    "device_authorization_endpoint",
    "resource_parameter",
    "resource_value",
    "authorization_params",
    "key_id",
    "registration_method",
    "token_endpoint_auth_signing_alg",
    "token_expiry_seconds",
}
SECRETS = {"client_secret", "private_key", "registration_access_token"}
PARAMS = {"prompt", "login_hint", "domain_hint", "access_type", "audience", "organization"}


def callback_url() -> str:
    from .mcp_http_oauth import https_url

    base = settings.public_url.rstrip("/")
    https_url(base)
    return base + "/api/mcp-oauth/web-callback"


def callback_url_if_configured() -> str | None:
    if not settings.public_url or not settings.public_url.startswith("https://"):
        return None
    return callback_url()


def metadata_client_url() -> str:
    return callback_url().rsplit("/", 1)[0] + "/client-metadata"


def validate(profile: dict) -> dict:
    result = dict(profile)
    defaults = {
        "grant_type": "authorization_code",
        "token_endpoint_auth_method": "none",
        "callback_mode": "desktop",
        "metadata_mode": "discovery",
        "resource_parameter": "resource" if profile.get("use_resource", True) else "none",
        "registration_method": "auto",
    }
    for key, default in defaults.items():
        result.setdefault(key, default)
    for key, choices in {
        "grant_type": GRANTS,
        "token_endpoint_auth_method": AUTH_METHODS,
        "callback_mode": {"desktop", "web"},
        "metadata_mode": {"discovery", "manual"},
        "resource_parameter": {"resource", "audience", "none"},
        "registration_method": {"auto", "manual", "dcr", "cimd"},
    }.items():
        if not isinstance(result[key], str) or result[key] not in choices:
            raise HTTPException(400, f"Unsupported OAuth {key}")
    method = result["token_endpoint_auth_method"]
    if result["grant_type"] == "client_credentials" and method == "none":
        raise HTTPException(400, "Service-account OAuth requires a confidential client")
    params = result.get("authorization_params", {})
    if isinstance(params, str):
        try:
            params = json.loads(params) if params else {}
        except ValueError as exc:
            raise HTTPException(400, "Authorization parameters must be a JSON object") from exc
    if (
        not isinstance(params, dict)
        or set(params) - PARAMS
        or any(
            not isinstance(v, str) or len(v) > 2048 or any(ord(c) < 32 for c in v) for v in params.values()
        )
    ):
        raise HTTPException(400, "Use only bounded, supported authorization parameters")
    result["authorization_params"] = params
    for key in ("resource_value", "key_id"):
        value = result.get(key, "")
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise HTTPException(400, f"Invalid OAuth {key}")
    expiry = result.get("token_expiry_seconds")
    if expiry not in (None, ""):
        if isinstance(expiry, bool) or not isinstance(expiry, int) or not 1 <= expiry <= 31536000:
            raise HTTPException(400, "Token expiry fallback must be an integer from 1 to 31536000 seconds")
    if result["callback_mode"] == "web":
        result["redirect_uri"] = callback_url()
    return result


def redact(profile: dict) -> dict:
    return {
        **{k: v for k, v in profile.items() if k not in SECRETS | {"sealed_client_credentials"}},
        **{f"{key}_configured": bool(credentials(profile).get(key)) for key in SECRETS},
    }


def credentials(profile: dict) -> dict:
    value = profile.get("sealed_client_credentials")
    return unseal(value, namespace="mcp-oauth") if value else {}


def configure_credentials(profile: dict, payload: dict, previous: dict | None) -> dict:
    # A client/issuer change must never silently send an old secret to a new provider.
    same_client = previous and all(
        previous.get(k) == profile.get(k) for k in ("issuer", "client_id", "token_endpoint")
    )
    values = credentials(previous) if same_client else {}
    for key in SECRETS:
        value = payload.get(key)
        if value in (None, ""):
            continue  # Password fields deliberately preserve a stored value when blank.
        if not isinstance(value, str) or len(value) > 16384 or "\0" in value:
            raise HTTPException(400, "Invalid OAuth client credential")
        if key == "registration_access_token" and any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise HTTPException(400, "Invalid OAuth registration access token")
        values[key] = value
    if payload.get("clear_client_credentials") is True:
        values = {}
    if values.get("private_key"):
        private_key(values["private_key"])
    result = {**profile}
    result.pop("sealed_client_credentials", None)
    if values:
        from .mcp_http_oauth import ready_key

        ready_key()
        result["sealed_client_credentials"] = seal(values, namespace="mcp-oauth")
    return result


def private_key(value):
    try:
        key = serialization.load_pem_private_key(value.encode(), password=None)
        if isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 2048:
            return key, "RS256"
        if isinstance(key, ec.EllipticCurvePrivateKey):
            alg = {"secp256r1": "ES256", "secp384r1": "ES384", "secp521r1": "ES512"}.get(key.curve.name)
            if alg:
                return key, alg
        if isinstance(key, (ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)):
            return key, "EdDSA"
    except (ValueError, TypeError):
        pass
    raise HTTPException(400, "Use an unencrypted RSA (2048+ bit), P-256/P-384/P-521 or EdDSA signing key")


def require_credentials(profile):
    method = profile.get("token_endpoint_auth_method", "none")
    values = credentials(profile)
    required = "private_key" if method == "private_key_jwt" else "client_secret"
    if method != "none" and not values.get(required):
        raise HTTPException(400, f"This OAuth client requires a stored {required}")
    if method in {"private_key_jwt", "client_secret_jwt"}:
        signing_key(profile, values)


def signing_key(profile, values):
    method = profile["token_endpoint_auth_method"]
    if method == "private_key_jwt":
        key, default = private_key(values["private_key"])
        allowed = ["RS256", "PS256", "RS384", "PS384", "RS512", "PS512"] if default == "RS256" else [default]
    else:
        key, allowed = values["client_secret"], ["HS256", "HS384", "HS512"]
    advertised = profile.get("token_endpoint_auth_signing_alg_values_supported", [])
    requested = profile.get("token_endpoint_auth_signing_alg")
    alg = requested or next((a for a in allowed if not advertised or a in advertised), None)
    if alg not in allowed or advertised and alg not in advertised:
        raise HTTPException(400, "Signing algorithm is not supported by both this client's key and provider")
    if alg.startswith("HS") and len(key.encode()) < int(alg[2:]) // 8:
        raise HTTPException(400, "Client secret is too short for the selected HMAC signing algorithm")
    return key, alg


def resource_fields(profile) -> dict:
    parameter = profile.get("resource_parameter", "resource" if profile.get("use_resource", True) else "none")
    return {} if parameter == "none" else {parameter: profile.get("resource_value") or profile["resource"]}


def authenticate(profile, data):
    """Return a fresh token form + optional Authorization header; never mutate caller data."""
    require_credentials(profile)
    form, headers = dict(data), {}
    client, method = profile["client_id"], profile.get("token_endpoint_auth_method", "none")
    values = credentials(profile)
    form["client_id"] = client
    if method == "client_secret_basic":
        raw = f"{quote_plus(client, safe='')}:{quote_plus(values['client_secret'], safe='')}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        form.pop("client_id", None)
    elif method == "client_secret_post":
        form["client_secret"] = values["client_secret"]
    elif method in {"private_key_jwt", "client_secret_jwt"}:
        key, alg = signing_key(profile, values)
        now = int(time.time())
        form.update(
            {
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": jwt.encode(
                    {
                        "iss": client,
                        "sub": client,
                        "aud": profile["token_endpoint"],
                        "iat": now,
                        "exp": now + 60,
                        "jti": secrets.token_urlsafe(32),
                    },
                    key,
                    algorithm=alg,
                    headers={"kid": profile["key_id"]} if profile.get("key_id") else None,
                ),
            }
        )
    return form, headers
