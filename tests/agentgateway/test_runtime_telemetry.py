"""Supervisor telemetry exports only fixed operation labels and trace correlation."""

import importlib.util
import json
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "services/agentgateway"


def module(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_stdio_span_redacts_the_process_and_marks_timeout(monkeypatch):
    monkeypatch.setitem(sys.modules, "stdio_supervisor", module("stdio_supervisor"))
    telemetry = module("runtime_telemetry")
    event = {
        "target": "brave-search",
        "outcome": "timeout",
        "method": "initialize",
        "deadline_ms": 20_000,
        "started_ns": 100,
        "ended_ns": 400,
        "arguments": "do-not-export",
        "traceparent": f"00-{'a' * 32}-{'b' * 16}-01",
    }
    span = telemetry.startup_span(event)
    assert span["traceId"] == "a" * 32
    assert span["parentSpanId"] == "b" * 16
    assert span["status"] == {"code": 2}
    assert "do-not-export" not in json.dumps(span)
    assert telemetry.startup_span({**event, "method": "do-not-export"}) is None


def test_gateway_gauge_counts_live_managed_group_leaders(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "stdio_supervisor", module("stdio_supervisor"))
    telemetry = module("runtime_telemetry")
    for pid, comm, parent, group, state in (
        (2, "stdio_superviso", 1, 1, "S"),
        (3, "node", 2, 3, "S"),
        (4, "other", 3, 3, "R"),
        (5, "node", 2, 5, "Z"),
    ):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(f"{pid} ({comm}) {state} {parent} {group} 0\n")
        (directory / "comm").write_text(comm + "\n")
    assert telemetry.live_managed_children(tmp_path) == 1


def test_bundled_brave_starts_node_directly_without_changing_custom_launches():
    supervisor = module("stdio_supervisor")
    config = json.loads((SOURCE / "brave/package.json").read_text())
    lock = json.loads((SOURCE / "brave/package-lock.json").read_text())
    name = supervisor.BRAVE_PACKAGE
    version = config["dependencies"][name]
    assert lock["packages"]["node_modules/" + name]["version"] == version
    assert supervisor.BRAVE_ARGS[1] == f"{name}@{version}"
    assert supervisor.launch(["npx", *supervisor.BRAVE_ARGS]) == [
        "/usr/local/bin/node",
        supervisor.BRAVE_ENTRY,
        "--transport",
        "stdio",
    ]
    custom = ["npx", "-y", f"{name}@999.0.1", "--transport", "stdio"]
    assert supervisor.launch(custom) is custom
