"""Generic MCP OAuth grants, registration and secret-boundary regressions."""

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from nautionette_backend import mcp_http_oauth as oauth
from nautionette_backend import mcp_oauth_provider as provider
from nautionette_backend.config import settings

from tests.backend.test_mcp_http_oauth import BASE, ISSUER, URL, forward, hosted  # noqa: F401


@pytest.fixture
def generic(hosted, monkeypatch):  # noqa: F811 - imported pytest fixture
    original = oauth.fetch
    hosted.requests = []
    hosted.device_responses = []
    hosted.metadata["token_endpoint_auth_methods_supported"] = sorted(provider.AUTH_METHODS)
    hosted.metadata["device_authorization_endpoint"] = ISSUER + "/device"
    monkeypatch.setattr(settings, "public_url", "https://app.example.test")

    async def fetch(url, data=None, *, headers=None, json_body=None):
        if data is not None or json_body is not None:
            hosted.requests.append((url, data, headers, json_body))
        if url == ISSUER + "/register":
            return (
                201,
                {},
                {
                    "client_id": "dcr-client",
                    "client_secret": "issued-secret",
                    "redirect_uris": json_body.get("redirect_uris", []),
                    "token_endpoint_auth_method": json_body["token_endpoint_auth_method"],
                },
            )
        if url == ISSUER + "/device":
            return (
                200,
                {},
                {
                    "device_code": "private-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": ISSUER + "/verify",
                    "expires_in": 500,
                    "interval": 5,
                },
            )
        if data and data.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
            if hosted.device_responses:
                result = hosted.device_responses.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
        return await original(url, data)

    monkeypatch.setattr(oauth, "fetch", fetch)
    return hosted


def prepare(client, **changes):
    response = client.post(
        BASE + "/prepare/example",
        json={
            "url": URL,
            "client_id": "client",
            "redirect_uri": "http://localhost:12798",
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def start(client, config):
    response = client.post(BASE + "/start/example", json=config)
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post", "client_secret_jwt"])
def test_confidential_clients_are_sealed_and_authenticate_at_token_endpoint(client, backend, generic, method):
    secret = "private-client-secret-for-provider-tests"
    config = prepare(client, client_secret=secret, token_endpoint_auth_method=method)
    assert config["client_secret_configured"]
    assert secret not in json.dumps(config)
    stored = backend.db.one("SELECT profile FROM mcp_oauth_setups")["profile"]
    assert secret not in stored
    identifier = start(client, {**config, "desktop_bridge_version": 1})
    auth = client.get(BASE + "/login/" + identifier).json()["authorization"]
    assert forward(client, identifier, auth).status_code == 200
    _, data, headers, _ = generic.requests[-1]
    if method == "client_secret_basic":
        assert headers["Authorization"] == "Basic " + base64.b64encode(f"client:{secret}".encode()).decode()
        assert "client_id" not in data and "client_secret" not in data
    elif method == "client_secret_post":
        assert data["client_secret"] == secret
    else:
        claims = jwt.decode(
            data["client_assertion"],
            secret,
            algorithms=["HS256"],
            audience=ISSUER + "/token",
        )
        assert claims["iss"] == claims["sub"] == "client"
        assert claims["exp"] - claims["iat"] == 60
    assert client.get(BASE + "/login/" + identifier).json()["status"] == "complete"
    assert secret not in client.get("/api/mcp-servers").text


def test_private_key_assertion_is_fresh_signed_and_not_exposed(client, generic):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    config = prepare(client, private_key=pem, key_id="key-1", token_endpoint_auth_method="private_key_jwt")
    assert config["private_key_configured"] and "PRIVATE KEY" not in json.dumps(config)
    identifier = start(client, {**config, "desktop_bridge_version": 1})
    auth = client.get(BASE + "/login/" + identifier).json()["authorization"]
    assert forward(client, identifier, auth).status_code == 200
    assertion = generic.requests[-1][1]["client_assertion"]
    claims = jwt.decode(assertion, key.public_key(), algorithms=["RS256"], audience=ISSUER + "/token")
    assert claims["sub"] == "client" and jwt.get_unverified_header(assertion)["kid"] == "key-1"
    assert client.get(BASE + "/login/" + identifier).json()["status"] == "complete"


def test_blank_preserves_secret_but_client_change_drops_it(client, generic):
    prepare(client, client_secret="stored-secret", token_endpoint_auth_method="client_secret_post")
    assert prepare(client, client_secret="")["client_secret_configured"]
    config = prepare(client, client_id="other-client", client_secret="")
    assert not config["client_secret_configured"]
    response = client.post(BASE + "/start/example", json={**config, "desktop_bridge_version": 1})
    assert response.status_code == 400 and not generic.requests


def test_start_cannot_swap_client_and_reuse_saved_secret(client, generic):
    config = prepare(client, client_secret="stored-secret", token_endpoint_auth_method="client_secret_post")
    response = client.post(
        BASE + "/start/example", json={**config, "client_id": "attacker", "desktop_bridge_version": 1}
    )
    assert response.status_code == 409 and not generic.requests


@pytest.mark.parametrize("mode", ["query", "form_post"])
def test_web_callback_needs_exact_state_is_one_shot_and_does_not_publish(
    client, anonymous, backend, generic, mode
):
    config = prepare(client, callback_mode="web", response_mode=mode)
    assert config["redirect_uri"] == provider.callback_url()
    identifier = start(client, config)
    auth = client.get(BASE + "/login/" + identifier).json()["authorization"]
    assert auth["callback_mode"] == "web"
    fields = {"state": auth["state"], "code": "web-code", "iss": ISSUER}
    path = BASE + "/web-callback"
    assert anonymous.get(path, params={**fields, "state": "wrong"}).status_code == 400
    response = (
        anonymous.get(path, params=fields)
        if mode == "query"
        else anonymous.post(
            path, content=urlencode(fields), headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
    )
    assert response.status_code == 200
    assert "web-code" not in response.text and auth["state"] not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert not backend.gateway.resources.get("mcp.target")
    assert anonymous.get(path, params=fields).status_code == 400
    assert client.get(BASE + "/login/" + identifier).json()["status"] == "complete"
    assert len(generic.requests) == 1


def test_web_return_rejects_duplicate_fields_and_cancelled_login(client, anonymous, generic):
    identifier = start(client, prepare(client, callback_mode="web"))
    auth = client.get(BASE + "/login/" + identifier).json()["authorization"]
    query = urlencode({"state": auth["state"], "code": "one"}) + "&code=two"
    assert anonymous.get(BASE + "/web-callback?" + query).status_code == 400
    assert client.delete(BASE + "/login/" + identifier).status_code == 204
    assert (
        anonymous.get(BASE + "/web-callback", params={"state": auth["state"], "code": "one"}).status_code
        == 400
    )
    assert not generic.requests


def test_service_account_connects_and_renews_without_desktop_or_refresh_token(client, backend, generic):
    generic.token.pop("refresh_token")
    config = prepare(
        client,
        grant_type="client_credentials",
        token_endpoint_auth_method="client_secret_post",
        client_secret="service-secret",
    )
    identifier = start(client, config)
    assert generic.requests[-1][1]["grant_type"] == "client_credentials"
    assert client.get(BASE + "/login/" + identifier).json()["status"] == "complete"
    generic.clock[0] += 260
    generic.token["access_token"] = "access-token-2"
    import asyncio

    asyncio.run(oauth.refresh_once())
    assert generic.requests[-1][1]["grant_type"] == "client_credentials"
    assert backend.db.one("SELECT status FROM mcp_http_oauth")["status"] == "connected"
    assert len(generic.requests) == 2


def test_device_poll_enforces_interval_and_slow_down_without_desktop(client, generic):
    generic.device_responses = [
        (400, {}, {"error": "authorization_pending"}),
        (400, {}, {"error": "slow_down"}),
        (200, {}, generic.token),
    ]
    identifier = start(client, prepare(client, grant_type="device_code"))

    def poll():
        return client.get(BASE + "/login/" + identifier)

    response = poll().json()
    assert response["device"]["user_code"] == "ABCD-EFGH"
    assert "private-device-code" not in json.dumps(response)
    for _ in range(3):
        assert poll().status_code == 200
    assert len(generic.requests) == 1
    generic.clock[0] += 5
    assert poll().json()["status"] == "authorizing"
    generic.clock[0] += 5
    assert poll().json()["device"]["interval"] == 10
    generic.clock[0] += 5
    assert poll().json()["status"] == "authorizing"
    assert len(generic.requests) == 3
    generic.clock[0] += 5
    assert poll().json()["status"] == "complete"
    assert len(generic.requests) == 4


def test_uncertain_device_exchange_never_replays(client, generic):
    generic.device_responses = [HTTPException(502, "uncertain exchange")]
    identifier = start(client, prepare(client, grant_type="device_code"))
    generic.clock[0] += 5
    assert client.get(BASE + "/login/" + identifier).status_code == 502
    assert client.get(BASE + "/login/" + identifier).json()["status"] == "failed"
    assert len(generic.requests) == 2


def test_manual_provider_without_mcp_metadata_uses_validated_endpoints(client, generic):
    config = prepare(
        client,
        metadata_mode="manual",
        issuer=ISSUER,
        authorization_endpoint=ISSUER + "/custom-auth",
        token_endpoint=ISSUER + "/custom-token",
        resource_parameter="audience",
        resource_value="api-id",
        authorization_params={"prompt": "consent", "login_hint": "user@example.test"},
    )
    identifier = start(client, {**config, "desktop_bridge_version": 1})
    auth = client.get(BASE + "/login/" + identifier).json()["authorization"]
    params = parse_qs(urlsplit(auth["authorization_url"]).query)
    assert params["audience"] == ["api-id"] and "resource" not in params
    assert params["prompt"] == ["consent"]


@pytest.mark.parametrize(
    "changes",
    [
        {"authorization_params": {"redirect_uri": "https://attacker.test"}},
        {"authorization_params": {"state": "attacker"}},
        {"grant_type": "password"},
        {"grant_type": "implicit"},
        {"token_endpoint_auth_method": "unknown"},
        {"grant_type": "client_credentials", "token_endpoint_auth_method": "none"},
        {"private_key": "not a private key"},
    ],
)
def test_invalid_provider_options_fail_before_token_request(client, generic, changes):
    response = client.post(BASE + "/prepare/example", json={"url": URL, "client_id": "client", **changes})
    assert response.status_code == 400 and not generic.requests


def test_confidential_dynamic_registration_seals_returned_secret(client, backend, generic):
    generic.metadata["registration_endpoint"] = ISSUER + "/register"
    config = prepare(
        client,
        client_id="",
        token_endpoint_auth_method="client_secret_post",
        registration_access_token="registration-secret",
    )
    identifier = start(client, {**config, "desktop_bridge_version": 1})
    request = generic.requests[0]
    assert request[2]["Authorization"] == "Bearer registration-secret"
    assert request[3]["token_endpoint_auth_method"] == "client_secret_post"
    assert request[3]["application_type"] == "native"
    auth = client.get(BASE + "/login/" + identifier).json()["authorization"]
    assert forward(client, identifier, auth).status_code == 200
    assert generic.requests[-1][1]["client_secret"] == "issued-secret"
    assert "issued-secret" not in backend.db.one("SELECT profile FROM mcp_oauth_setups")["profile"]


def test_metadata_client_registration_uses_public_document_not_dcr(client, anonymous, generic):
    generic.metadata["client_id_metadata_document_supported"] = True
    config = prepare(client, client_id="", callback_mode="web", registration_method="cimd")
    assert config["client_id"] == provider.metadata_client_url()
    document = anonymous.get(BASE + "/client-metadata").json()
    assert document["client_id"] == config["client_id"]
    assert document["redirect_uris"] == [config["redirect_uri"]]
    assert document["token_endpoint_auth_method"] == "none"
    start(client, config)
    assert not generic.requests


def test_missing_key_with_only_saved_client_credentials_does_not_mint_replacement(client, generic):
    prepare(client, token_endpoint_auth_method="client_secret_post", client_secret="saved-secret")
    key = Path(settings.data_dir) / "mcp-oauth.key"
    key.unlink()
    response = client.post(
        BASE + "/prepare/other",
        json={
            "url": URL,
            "client_id": "other",
            "token_endpoint_auth_method": "client_secret_post",
            "client_secret": "new-secret",
        },
    )
    assert response.status_code == 503 and not key.exists()


def test_issuer_switch_does_not_inherit_registration_or_secret(client, backend, generic):
    prepare(
        client,
        metadata_mode="manual",
        issuer=ISSUER,
        authorization_endpoint=ISSUER + "/authorize",
        token_endpoint=ISSUER + "/token",
        token_endpoint_auth_method="client_secret_post",
        client_secret="issuer-bound-secret",
    )
    other = "https://other.example.test"
    response = client.post(
        BASE + "/prepare/example",
        json={
            "url": URL,
            "metadata_mode": "manual",
            "issuer": other,
            "authorization_endpoint": other + "/authorize",
            "token_endpoint": other + "/token",
        },
    )
    assert response.status_code == 400
    stored = json.loads(backend.db.one("SELECT profile FROM mcp_oauth_setups")["profile"])
    assert stored["client_id"] == "" and "sealed_client_credentials" not in stored
    assert not generic.requests


def test_public_callback_rejects_demoted_initiator(client, anonymous, backend, generic):
    identifier = start(client, prepare(client, callback_mode="web"))
    auth = client.get(BASE + "/login/" + identifier).json()["authorization"]
    backend.db.execute("UPDATE auth_users SET role='member' WHERE id='test-admin'")
    response = anonymous.get(BASE + "/web-callback", params={"state": auth["state"], "code": "test-code"})
    assert response.status_code == 400 and not generic.requests


def test_missing_advertised_issuer_does_not_consume_authorization_code(client, generic):
    generic.metadata["authorization_response_iss_parameter_supported"] = True
    identifier = start(client, {**prepare(client), "desktop_bridge_version": 1})
    auth = client.get(BASE + "/login/" + identifier).json()["authorization"]
    assert forward(client, identifier, auth).status_code == 400
    assert not generic.requests
    assert forward(client, identifier, auth, iss=ISSUER).status_code == 200


def test_challenged_metadata_and_scopes_take_precedence(client, generic, monkeypatch):
    original = oauth.fetch
    read_urls = []

    async def fetch(url, data=None, **kwargs):
        read_urls.append(url)
        if url == URL:
            return (
                401,
                {"WWW-Authenticate": f'Bearer resource_metadata="{ISSUER}/protected", scope="narrow"'},
                {},
            )
        if url == ISSUER + "/protected":
            return 200, {}, generic.resource
        return await original(url, data, **kwargs)

    monkeypatch.setattr(oauth, "fetch", fetch)
    config = prepare(client)
    assert read_urls[:2] == [URL, ISSUER + "/protected"]
    assert config["scope"] == "narrow offline_access"


def test_provider_without_token_expiry_requires_explicit_fallback(client, generic):
    generic.token.pop("expires_in")
    config = prepare(
        client,
        grant_type="client_credentials",
        token_endpoint_auth_method="client_secret_post",
        client_secret="service-secret",
        token_expiry_seconds=1800,
    )
    identifier = start(client, config)
    assert oauth._sessions[identifier].tokens["expires_at"] == generic.clock[0] + 1800
    assert client.get(BASE + "/login/" + identifier).json()["status"] == "complete"


@pytest.mark.parametrize("algorithm", ["RS256", "PS256", "RS384", "PS512"])
def test_signing_assertions_match_provider_algorithm_and_have_unique_jti(client, generic, algorithm):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    generic.metadata["token_endpoint_auth_signing_alg_values_supported"] = [algorithm]
    prepare(client, private_key=pem, token_endpoint_auth_method="private_key_jwt")
    from nautionette_backend.mcp_oauth_setup import saved

    profile = saved("example")["profile"]
    assertions = [provider.authenticate(profile, {})[0]["client_assertion"] for _ in range(2)]
    claims = [
        jwt.decode(value, key.public_key(), algorithms=[algorithm], audience=ISSUER + "/token")
        for value in assertions
    ]
    assert claims[0]["jti"] != claims[1]["jti"]


def test_login_owner_check_rejects_other_accounts_without_consuming_login(client, generic):
    from nautionette_backend.security import Principal, current_principal

    identifier = start(client, prepare(client, callback_mode="web"))
    reset = current_principal.set(Principal("other-admin", "admin"))
    try:
        with pytest.raises(HTTPException) as error:
            oauth.session(identifier)
        assert error.value.status_code == 403
    finally:
        current_principal.reset(reset)
    assert identifier in oauth._sessions and not generic.requests


@pytest.mark.parametrize("grant", ["device_code", "client_credentials"])
def test_noninteractive_grants_do_not_require_a_bound_callback(client, generic, grant):
    response = client.post(
        BASE + "/prepare/example",
        json={
            "url": URL,
            "client_id": "client",
            "grant_type": grant,
            **(
                {"token_endpoint_auth_method": "client_secret_post", "client_secret": "service-secret"}
                if grant == "client_credentials"
                else {}
            ),
        },
    )
    assert response.status_code == 200, response.text
    config = response.json()
    assert config["redirect_uri"] == "http://127.0.0.1:0/callback"
    identifier = start(client, config)
    assert client.get(BASE + "/login/" + identifier).status_code == 200


def test_entra_service_account_preserves_default_scope_without_oidc_scopes(client, generic):
    issuer = "https://login.microsoftonline.com/common/v2.0"
    response = client.post(
        BASE + "/prepare/example",
        json={
            "url": URL,
            "client_id": "client",
            "grant_type": "client_credentials",
            "metadata_mode": "manual",
            "issuer": issuer,
            "token_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "token_endpoint_auth_method": "client_secret_post",
            "client_secret": "service-secret",
            "scope": "https://resource.example/.default",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "https://resource.example/.default"
    start(client, response.json())
    assert generic.requests[-1][1]["scope"] == "https://resource.example/.default"


@pytest.mark.parametrize(
    "query", ["code_challenge_method=plain", "code_challenge_method=", "response_mode=form_post", "state="]
)
def test_web_authorization_rejects_duplicate_protocol_parameters(client, generic, query):
    generic.metadata["authorization_endpoint"] = ISSUER + "/authorize?" + query
    response = client.post(
        BASE + "/prepare/example", json={"url": URL, "client_id": "client", "callback_mode": "web"}
    )
    assert response.status_code == 400 and not generic.requests and not oauth._sessions


def test_changed_authenticated_device_destination_drops_stored_client_secret(client, generic):
    prepare(
        client,
        grant_type="device_code",
        token_endpoint_auth_method="client_secret_post",
        client_secret="device-client-secret",
    )
    generic.metadata["device_authorization_endpoint"] = "https://other.example.test/device"
    config = prepare(client, refresh_metadata=True)
    assert not config["client_secret_configured"]
    response = client.post(BASE + "/start/example", json=config)
    assert response.status_code == 400 and not generic.requests


def test_changed_registration_destination_drops_stored_access_token(client, generic):
    generic.metadata["registration_endpoint"] = ISSUER + "/register"
    assert prepare(client, client_id="", registration_access_token="registration-secret")[
        "registration_access_token_configured"
    ]
    generic.metadata["registration_endpoint"] = "https://other.example.test/register"
    config = prepare(client, client_id="", refresh_metadata=True)
    assert not config["registration_access_token_configured"] and not generic.requests


@pytest.mark.parametrize(
    "endpoint_key", ["token_endpoint", "device_authorization_endpoint", "registration_endpoint"]
)
def test_encrypted_bindings_are_checked_immediately_before_using_credentials(client, generic, endpoint_key):
    from nautionette_backend.mcp_oauth_setup import saved

    generic.metadata["registration_endpoint"] = ISSUER + "/register"
    prepare(
        client,
        grant_type="device_code",
        token_endpoint_auth_method="client_secret_post",
        client_secret="client-secret",
        registration_access_token="registration-secret",
    )
    profile = saved("example")["profile"]
    profile[endpoint_key] = "https://other.example.test/destination"
    with pytest.raises(HTTPException) as error:
        if endpoint_key == "registration_endpoint":
            provider.registration_token(profile)
        elif endpoint_key == "device_authorization_endpoint":
            provider.authenticate(profile, {}, endpoint=profile[endpoint_key])
        else:
            provider.authenticate(profile, {})
    assert error.value.status_code == 400 and not generic.requests


@pytest.mark.asyncio
async def test_public_callback_body_read_has_a_sanitized_deadline(generic, monkeypatch):
    import asyncio

    from nautionette_backend.routers import mcp_oauth_public
    from starlette.requests import Request

    monkeypatch.setattr(mcp_oauth_public, "CALLBACK_BODY_SECONDS", 0.01)

    async def stalled():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"private-code", "more_body": True}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/mcp-oauth/web-callback",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "query_string": b"",
            "scheme": "https",
            "server": ("app.example.test", 443),
        },
        stalled,
    )
    result = await mcp_oauth_public.browser_return(request)
    assert result.status_code == 400 and b"private-code" not in result.body
    assert result.headers["cache-control"] == "no-store" and not generic.requests


def test_device_dynamic_registration_omits_unused_callback_fields(client, generic):
    generic.metadata["registration_endpoint"] = ISSUER + "/register"
    response = client.post(BASE + "/prepare/example", json={"url": URL, "grant_type": "device_code"})
    assert response.status_code == 200, response.text
    start(client, response.json())
    metadata = generic.requests[0][3]
    assert metadata["application_type"] == "native"
    assert "redirect_uris" not in metadata and "response_types" not in metadata
    assert metadata["grant_types"][0] == "urn:ietf:params:oauth:grant-type:device_code"


def test_web_dynamic_registration_uses_web_application_type(client, generic):
    generic.metadata["registration_endpoint"] = ISSUER + "/register"
    config = prepare(client, client_id="", callback_mode="web", registration_access_token="initial-token")
    identifier = start(client, config)
    assert generic.requests[0][3]["application_type"] == "web"
    assert generic.requests[0][3]["redirect_uris"] == [provider.callback_url()]
    assert not provider.credentials(oauth._sessions[identifier].profile).get("registration_access_token")
