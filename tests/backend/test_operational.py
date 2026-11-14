"""Service readiness is separate from human sessions and private storage metadata."""

import pytest
from fastapi.testclient import TestClient
from nautionette_backend import main, output_storage
from nautionette_backend.config import settings

from ..conftest import APP_TOKEN, INTERNAL_TOKEN


@pytest.mark.parametrize("path", ["/internal/system", "/internal/settings/storage"])
def test_operational_endpoints_require_only_service_protocol(backend, path):
    http = TestClient(main.app, base_url="https://testserver")
    assert http.get(path).status_code == 401
    assert (
        http.post(
            "/api/auth/login",
            json={"username": "admin", "password": "a correct horse battery staple for tests"},
        ).status_code
        == 200
    )
    assert http.cookies.get("dz_session")
    assert http.get(path).status_code == 401
    assert http.get(path, headers={"Authorization": "Bearer " + APP_TOKEN}).status_code == 401
    assert http.get(path, headers={"X-Internal-Token": "wrong"}).status_code == 401
    response = http.get(path, headers={"X-Internal-Token": INTERNAL_TOKEN})
    assert response.status_code == 200
    if path == "/internal/system":
        assert response.json()["auth_enabled"] is True
        assert {item["name"] for item in response.json()["components"]} == {
            "temporal",
            "broker",
            "agentgateway",
            "workflow-mcp",
        }


def test_operational_storage_removes_diagnostics_names_and_configuration(backend, monkeypatch):
    monkeypatch.setattr(
        output_storage,
        "status",
        lambda: {
            "health": {"status": "unavailable", "error": "private-provider-diagnostic"},
            "bucket": "private-bucket",
            "endpoint": "https://private.example",
            "policy": {"private": True},
            "maintenance": {"error": "private-maintenance"},
            "filesystems": [
                {
                    "label": "MaxIO backing filesystem",
                    "available": True,
                    "total_bytes": 100,
                    "used_bytes": 40,
                    "available_bytes": 60,
                    "path": "/private-volume",
                },
                {"label": "private-name", "available": True, "total_bytes": 1},
            ],
        },
    )
    response = TestClient(main.app).get(
        "/internal/settings/storage", headers={"X-Internal-Token": INTERNAL_TOKEN}
    )
    assert response.status_code == 200
    assert "private" not in response.text
    assert response.json() == {
        "health": {"status": "unavailable"},
        "filesystems": [
            {
                "label": "MaxIO backing filesystem",
                "available": True,
                "total_bytes": 100,
                "used_bytes": 40,
                "available_bytes": 60,
            }
        ],
    }


def test_operational_service_token_fails_closed_when_unconfigured(backend, monkeypatch):
    monkeypatch.setattr(settings, "internal_token", "")
    assert (
        TestClient(main.app)
        .get("/internal/settings/storage", headers={"X-Internal-Token": INTERNAL_TOKEN})
        .status_code
        == 401
    )


def test_operational_routes_are_not_in_human_or_agent_api_schema():
    paths = main.app.openapi()["paths"]
    assert "/internal/system" not in paths
    assert "/internal/settings/storage" not in paths
