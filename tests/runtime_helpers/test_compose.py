"""Shared deployment invariants retained after retiring the Python broker."""

import re
import runpy

import yaml

from scripts import image_context


def test_ci_outputs_exactly_the_brokers_source_fingerprints(repo_root, monkeypatch, capsys):
    monkeypatch.chdir(repo_root)
    script = repo_root / "scripts/image_context.py"
    monkeypatch.setattr("sys.argv", [str(script)])
    runpy.run_path(str(script), run_name="__main__")
    outputs = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert outputs == {
        "base": image_context.base_hash(repo_root / "images"),
        "default": image_context.agent_set_hash(repo_root / "images", "default"),
    }


def test_resource_defaults_match_compose_and_example(repo_root):
    runtime = (repo_root / "services/docker-broker-rust/src/runtime.rs").read_text()
    environment = yaml.safe_load((repo_root / "docker-compose.yaml").read_text())["services"][
        "docker-broker"
    ]["environment"]
    example = (repo_root / ".env.example").read_text().splitlines()
    for key, constant, value in [
        ("AGENT_MEMORY_LIMIT", "DEFAULT_MEMORY", "4g"),
        ("AGENT_WORKSPACE_SIZE", "DEFAULT_WORKSPACE_SIZE", "1g"),
    ]:
        assert re.search(rf'const {constant}: &str = "{value}"', runtime)
        assert environment[key] == "${" + key + ":-" + value + "}"
        assert f"{key}={value}" in example


def test_every_deployed_compose_service_has_a_healthcheck(repo_root):
    compose = yaml.safe_load((repo_root / "docker-compose.yaml").read_text())
    deployed = {
        name
        for name, service in compose["services"].items()
        if not set(service.get("profiles", [])) & {"debug", "images", "maintenance"}
    }
    assert deployed == {
        "app-sandbox",
        "agentgateway",
        "backend",
        "docker-broker",
        "frontend-web",
        "maxio",
        "otel-collector",
        "postgres",
        "stt",
        "temporal",
        "website",
        "worker",
        "workflow-mcp",
    }
    assert all(compose["services"][name].get("healthcheck", {}).get("test") for name in deployed)


def test_stt_compose_is_self_hosted_bounded_and_never_gates_chat_startup(repo_root):
    compose = yaml.safe_load((repo_root / "docker-compose.yaml").read_text())
    services = compose["services"]
    stt, backend = services["stt"], services["backend"]
    assert not stt.get("ports")
    assert stt["networks"] == ["internal"]
    assert not stt.get("environment")
    assert stt["cpus"] == "${STT_CPUS:-4}"
    assert stt["mem_limit"] == "${STT_MEMORY:-3g}"
    assert "stt-models:/models" in stt["volumes"]
    assert any(path.startswith("/tmp:") for path in stt["tmpfs"])  # noqa: S108 - assert RAM-backed uploads
    assert stt["healthcheck"]["start_period"] == "600s"
    assert "stt" not in backend["depends_on"]
    assert not {"STT_BASE_URL", "STT_MODEL", "STT_API_KEY"} & backend["environment"].keys()
    dockerfile = (repo_root / "services/stt/Dockerfile").read_text()
    assert "USER speech" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--ws-max-size" in dockerfile
    assert "--ws-max-queue" in dockerfile


def test_maxio_compose_is_private_pinned_persistent_and_not_an_agent_credential_delivery(repo_root):
    compose = yaml.safe_load((repo_root / "docker-compose.yaml").read_text())
    services = compose["services"]
    maxio, backend = services["maxio"], services["backend"]
    assert maxio["image"] == "${STACK_NAMESPACE:-nautionette}/maxio:dev"
    assert maxio["build"] == {
        "context": "./infra/maxio",
        "args": {"MAXIO_VERSION": "${MAXIO_VERSION:-0.4.2}"},
    }
    dockerfile = (repo_root / "infra/maxio/Dockerfile").read_text()
    assert "ARG MAXIO_VERSION=0.4.2" in dockerfile
    assert "FROM ghcr.io/coollabsio/maxio:${MAXIO_VERSION}" in dockerfile
    assert dockerfile.rstrip().endswith("USER maxio:maxio")
    assert not maxio.get("ports")
    assert set(maxio["networks"]) == {"outputs", "agents"}
    assert compose["networks"]["outputs"]["internal"] is True
    assert "maxio-data:/data" in maxio["volumes"]
    assert "maxio-data:/output-storage:ro" in backend["volumes"]
    assert backend["environment"]["OUTPUT_STORAGE_DISK_PATH"] == "/output-storage"
    assert backend["environment"]["OUTPUT_S3_ENDPOINT"] == "http://maxio:9000"
    assert backend["depends_on"]["maxio"]["condition"] == "service_healthy"
    assert ":?" in maxio["environment"]["MAXIO_SECRET_KEY"]
    for name, service in services.items():
        if name not in {"backend", "maxio"}:
            assert not any(key.startswith(("MAXIO_", "OUTPUT_S3_")) for key in service.get("environment", {}))


def test_full_stack_smoke_provisions_separate_required_maxio_credentials(repo_root):
    workflow = yaml.safe_load((repo_root / ".github/workflows/smoke.yml").read_text())
    steps = workflow["jobs"]["smoke"]["steps"]
    configure = next(step["run"] for step in steps if step.get("name") == "Write .env")
    assert 'echo "MAXIO_ACCESS_KEY=nautionette-smoke"' in configure
    assert 'echo "MAXIO_SECRET_KEY=$(openssl rand -hex 32)"' in configure
    assert "private MaxIO output storage is connected" in (repo_root / "scripts/smoke.py").read_text()


def test_compose_sandbox_network_has_outbound_access_without_control_plane_membership(repo_root):
    compose = yaml.safe_load((repo_root / "docker-compose.yaml").read_text())
    networks = compose["networks"]
    assert not networks["agents"].get("internal", False)
    assert not networks["internal"].get("internal", False)  # Workflow agents also have outbound access.
    assert "agent-egress" not in networks
    assert networks["agents"]["name"] == "${STACK_NAMESPACE:-nautionette}-agent-sandboxes"
    services = compose["services"]
    assert services["docker-broker"]["environment"]["AGENT_NETWORK"] == networks["agents"]["name"]
    assert "AGENT_EGRESS_NETWORK" not in services["docker-broker"]["environment"]
    assert "agents" in services["agentgateway"]["networks"]
    assert "agents" not in services["backend"]["networks"]
    assert "agents" not in services["docker-broker"]["networks"]
    assert networks["control"]["internal"] is True
    assert networks["data"]["internal"] is True
