"""Removing the Python server must not remove native conformance coverage."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "services/docker-broker-rust"
SPEC = importlib.util.spec_from_file_location("broker_conformance", CRATE / "tests/check_parity.py")
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


def test_retired_reference_inventory_is_frozen_and_still_maps_every_scenario():
    found, hashes = checks.inventory()
    manifest = json.loads((CRATE / "tests/parity-map.json").read_text())
    assert len(found) == 147
    assert found == {row["reference"] for row in manifest["coverage"]}
    assert hashes == manifest["reference_files"]
    assert not (ROOT / "services/docker-broker/Dockerfile").exists()
    assert not list((ROOT / "services/docker-broker").rglob("*.py"))
    assert not (ROOT / "scripts/temporal_deploy_hook.py").exists()
    assert "nautionette-docker-broker" not in (ROOT / "uv.lock").read_text()
    assert (
        "nautionette_docker_broker"
        not in (ROOT / "libs/nautionette/src/nautionette/import_aliases.py").read_text()
    )


def test_reference_inventory_cannot_silently_change(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/reference-inventory.json").write_text('{"functions": []}')
    monkeypatch.setattr(checks, "CRATE", tmp_path)
    with pytest.raises(ValueError, match="reference inventory changed"):
        checks.inventory()


def test_ignored_or_failed_native_tests_cannot_be_used_as_passing_evidence(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("test required_docker_case ... ignored\ntest native_case ... ok\n")
    assert checks.rust_passes([log]) == {"native_case"}
    log.write_text("test native_case ... ok\ntest result: FAILED\n")
    with pytest.raises(ValueError, match="failing"):
        checks.rust_passes([log])


def test_skipped_shared_parametrization_cannot_be_masked_by_passing_sibling(tmp_path):
    report = tmp_path / "cases.xml"
    report.write_text(
        '<testsuite><testcase classname="tests.example" name="test_invariant[a]"/>'
        '<testcase classname="tests.example" name="test_invariant[b]"><skipped/></testcase></testsuite>'
    )
    assert checks.shared_passes([report]) == set()


def test_omitted_shared_parametrizations_cannot_be_masked_by_passing_sibling(tmp_path):
    report = tmp_path / "cases.xml"
    reference, parameters = next(iter(checks.SHARED_PARAMETER_IDS.items()))
    path, name = reference.split("::", 1)
    classname = path.removesuffix(".py").replace("/", ".")
    cases = [f'<testcase classname="{classname}" name="{name}[{p}]"/>' for p in parameters]
    report.write_text("<testsuite>" + cases[0] + "</testsuite>")
    assert reference not in checks.shared_passes([report])
    report.write_text("<testsuite>" + "".join(cases) + "</testsuite>")
    assert reference in checks.shared_passes([report])


def test_conformance_requires_execution_of_every_historical_scenario(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["check_parity", "--evidence"])
    with pytest.raises(SystemExit) as exc:
        checks.main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reference_functions"] == 147
    assert report["mapped_rust_scenarios"] == 84
    assert report["accounting_passed"] is False
    assert any("no passing shared execution" in e for e in report["errors"])
    assert any("no passing Rust execution" in e for e in report["errors"])


def test_ci_prepares_native_artifacts_and_has_no_python_broker_jobs():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert all(
        item["file"] != "services/docker-broker/Dockerfile"
        for item in ci["jobs"]["images"]["strategy"]["matrix"]["include"]
    )
    steps = ci["jobs"]["agent-images"]["steps"]
    install = next(
        i for i, step in enumerate(steps) if "Install pinned Rust toolchain" in step.get("name", "")
    )
    prepare = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Prepare and offline-reverify creative-app artifact volumes"
    )
    assert install < prepare
    assert steps[prepare]["env"]["BROKER_DOCKER_SOCKET"] == "/var/run/docker.sock"
    assert "prepare-test-artifacts" in (ROOT / "scripts/prepare_artcraft_test.py").read_text()
    assert "tests/runtime_helpers" in (CRATE / "tests/run-local-conformance.sh").read_text()
