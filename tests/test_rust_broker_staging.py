"""Rust is required in staging, but is not used in production."""

import copy
from pathlib import Path

import pytest
import yaml

from scripts import coolify_release, sync_rust_broker_staging

ROOT = Path(__file__).resolve().parents[1]


def test_staging_compose_changes_only_the_broker_build_recipe():
    production = yaml.safe_load((ROOT / "docker-compose.yaml").read_text())
    expected = copy.deepcopy(production)
    expected["services"]["docker-broker"]["build"]["dockerfile"] = (
        "services/docker-broker-rust/Dockerfile.staging"
    )
    assert yaml.safe_load((ROOT / "docker-compose.rust-staging.yaml").read_text()) == expected
    assert production["services"]["docker-broker"]["build"]["dockerfile"] == (
        "services/docker-broker/Dockerfile"
    )


def test_generated_wiring_tracks_both_canonical_images_and_the_base_compose():
    for path, content in sync_rust_broker_staging.generated(ROOT).items():
        assert path.read_text() == content
    image = (ROOT / "services/docker-broker-rust/Dockerfile.staging").read_text()
    assert 'CMD ["/broker", "--local-runtime"]' in image
    assert 'CMD ["uvicorn"' not in image
    assert "uv sync --locked --no-dev --package nautionette-docker-broker" in image
    assert "BROKER_BIND_ADDR=0.0.0.0" in image
    assert "COPY --from=compile /broker /broker" in image
    assert "HEALTHCHECK" in image and "urllib.request" in image
    # The unmodified trusted hooks still need both Docker SDK and Python/sh.
    hook = (ROOT / "scripts/temporal_deploy_hook.py").read_text()
    assert "import docker" in hook and '"docker-broker"' in hook


def test_automatic_deployment_requires_rust_only_in_staging():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    deploy = next(
        s for s in workflow["jobs"]["deploy"]["steps"] if s.get("name") == "Pin, deploy, and verify"
    )
    assert deploy["env"]["EXPECTED_BROKER_IMPLEMENTATION"] == (
        "${{ github.event_name == 'workflow_run' && 'rust' || '' }}"
    )
    # Production keeps the Python implementation.
    production = yaml.safe_load((ROOT / "docker-compose.yaml").read_text())
    assert production["services"]["docker-broker"]["build"]["dockerfile"] == (
        "services/docker-broker/Dockerfile"
    )
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert "rust-broker" in ci["jobs"]
    assert any(
        item["file"] == "services/docker-broker-rust/Dockerfile.staging"
        for item in ci["jobs"]["images"]["strategy"]["matrix"]["include"]
    )


@pytest.mark.parametrize("implementation", [None, "python", "rust"])
def test_deployment_health_enforces_an_explicit_broker_implementation(monkeypatch, implementation):
    monkeypatch.setenv("EXPECTED_BROKER_IMPLEMENTATION", "rust")
    monkeypatch.setenv("APP_URL", "https://staging.example.test")
    monkeypatch.setenv("INTERNAL_TOKEN", "synthetic-staging-test-token")
    detail = {} if implementation is None else {"implementation": implementation}
    state = {
        "environment": "staging",
        "auth_enabled": True,
        "components": [
            {"name": name, "status": "ok", "detail": detail if name == "broker" else {}}
            for name in ("temporal", "broker", "agentgateway", "workflow-mcp")
        ],
    }
    storage = {"health": {"status": "ok"}, "backend": "s3"}
    monkeypatch.setattr(
        coolify_release,
        "request",
        lambda url, *_args, **_kwargs: storage if url.endswith("/storage") else state,
    )
    if implementation == "rust":
        coolify_release.healthy("staging")
    else:
        with pytest.raises(RuntimeError, match="broker implementation"):
            coolify_release.healthy("staging")
