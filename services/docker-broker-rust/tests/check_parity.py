#!/usr/bin/env python3
"""Fail-closed accounting for the frozen Python broker scenario inventory.

A checked mapping is NOT proof that two implementations are equivalent. With
--evidence, this gate additionally requires passing executions of every mapped
Rust scenario and every unchanged shared Python invariant. It refuses to certify
logs containing failures, unmapped reference functions, source drift or skipped
mapped scenarios. Keep full logs from the same checkout when comparing results.
"""

import argparse
import ast
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CRATE = ROOT / "services/docker-broker-rust"


def inventory():
    found = set()
    hashes = {}
    for path in sorted((ROOT / "tests/broker").glob("test*.py")):
        name = path.relative_to(ROOT).as_posix()
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for item in ast.parse(path.read_text()).body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                found.add(f"{name}::{item.name}")
    return found, hashes


def rust_passes(paths):
    passed = set()
    for path in paths:
        text = path.read_text(errors="replace")
        if "test result: FAILED" in text or re.search(r"^error(?:\[|:)", text, re.M):
            raise ValueError(f"failing or uncompilable test log: {path}")
        pending = None
        for line in text.splitlines():
            match = re.match(r"^test (\S+) \.\.\. ?(.*)$", line)
            if match:
                name, result = match.groups()
                pending = None if result.startswith(("ignored", "FAILED")) else name
                if result.startswith("ok"):
                    passed.add(name)
                    pending = None
            elif line == "ok" and pending is not None:
                passed.add(pending)
                pending = None
    return passed


def shared_passes(paths):
    result = set()
    for path in paths:
        data = path.read_bytes()
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            raise ValueError(f"DTD/entity declarations are not allowed in test reports: {path}")
        root = ET.fromstring(data)  # noqa: S314 - local test output; DTD/entity declarations rejected above.
        for case in root.iter("testcase"):
            if any(case.find(name) is not None for name in ("failure", "error")):
                raise ValueError(f"failing Python reference case in {path}")
            if case.find("skipped") is not None:
                continue
            module = case.attrib["classname"].replace(".", "/") + ".py"
            result.add(f"{module}::{case.attrib['name'].split('[', 1)[0]}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence", action="store_true", help="require execution evidence; inventory-only is not parity"
    )
    parser.add_argument("--rust-log", action="append", type=Path, default=[])
    parser.add_argument("--python-junit", action="append", type=Path, default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    manifest = json.loads((CRATE / "tests/parity-map.json").read_text())
    found, hashes = inventory()
    rows = {item["reference"]: item for item in manifest["coverage"]}
    errors = []
    if len(rows) != len(manifest["coverage"]):
        errors.append("duplicate reference mapping")
    if rows.keys() != found:
        errors.append(
            f"mapping drift: missing={sorted(found - rows.keys())}, stale={sorted(rows.keys() - found)}"
        )
    if hashes != manifest["reference_files"]:
        errors.append("Python source changed; review all affected scenario and parameter mappings")
    rust_source = "\n".join(
        path.read_text() for base in [CRATE / "src", CRATE / "tests"] for path in base.rglob("*.rs")
    )
    for row in rows.values():
        if not row["shared"] and not row["rust"]:
            errors.append(f"no Rust scenario assigned: {row['reference']}")
        for name in row["rust"]:
            if not re.search(r"\bfn\s+" + re.escape(name.rsplit("::", 1)[-1]) + r"\s*\(", rust_source):
                errors.append(f"missing Rust test: {name}")
    passed = rust_passes(args.rust_log)
    shared = shared_passes(args.python_junit)
    if args.evidence:
        for row in rows.values():
            if row["shared"]:
                if row["reference"] not in shared:
                    errors.append(f"no passing shared execution: {row['reference']}")
            for name in row["rust"]:
                if not any(actual == name or actual.endswith("::" + name) for actual in passed):
                    errors.append(f"no passing Rust execution: {row['reference']} -> {name}")
    inputs = sorted(
        {
            *CRATE.glob("Cargo.*"),
            *(CRATE / "src").rglob("*.rs"),
            *(CRATE / "tests").rglob("*.rs"),
            *(CRATE / "tests").glob("*.py"),
            *(CRATE / "tests").glob("*.cjs"),
            *(CRATE / "tests").glob("*.sh"),
            *(CRATE / "tests").glob("Dockerfile*"),
            CRATE / "Dockerfile.spike",
            CRATE / "compose.local.yaml",
            CRATE / "tests/parity-map.json",
            ROOT / "images/source-fingerprints.json",
            ROOT / "images/pi-base/artcraft/catalog.json",
            ROOT / "services/docker-broker/nautionette_docker_broker/artcraft_offline.py",
            ROOT / "tests/agent/fixtures/chat-model.mjs",
            ROOT / "tests/agent/fixtures/chat-provider.ts",
        }
    )
    candidate = hashlib.sha256()
    for path in inputs:
        candidate.update(path.relative_to(ROOT).as_posix().encode())
        candidate.update(path.read_bytes())
    report = {
        "candidate_source_sha256": candidate.hexdigest(),
        "scope": manifest["scope"],
        "reference_functions": len(found),
        "shared_unchanged_invariants": sum(row["shared"] for row in rows.values()),
        "mapped_rust_scenarios": len({name for row in rows.values() for name in row["rust"]}),
        "passing_rust_executions": len(passed),
        "execution_evidence_required": args.evidence,
        "passed_rust_scenarios": sorted(passed),
        "reference_sources": hashes,
        "accounting_passed": not errors,
        "errors": errors,
        "limitations": (
            "Scenario accounting is not a claim of literal SDK, error-message, telemetry-schema "
            "or unsupported-platform identity; see PARITY.md."
        ),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(f"Parity accounting failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
