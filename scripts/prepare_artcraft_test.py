"""Prepare/reverify pinned CI artifacts through the production Rust installer.

Requires the pinned Rust toolchain, an explicit disposable BROKER_DOCKER_SOCKET,
WORKFLOWS_VOLUME namespace, and the already-built Pi image. Stdout contains only
Docker --mount arguments, emitted after a separate offline verification process.
"""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not os.environ.get("BROKER_DOCKER_SOCKET"):
        raise SystemExit("Set BROKER_DOCKER_SOCKET to a disposable local Docker socket")
    if not os.environ.get("WORKFLOWS_VOLUME"):
        raise SystemExit("Set WORKFLOWS_VOLUME to a disposable fixture namespace")
    cargo = shutil.which("cargo")
    if not cargo:
        raise SystemExit("The pinned Cargo toolchain is required")
    for mode in ("prepare-test-artifacts", "verify-test-artifacts"):
        subprocess.run(  # noqa: S603,S607 - fixed Cargo target and prepared fixture, never shell
            [
                cargo,
                "run",
                "--quiet",
                "--locked",
                "--manifest-path",
                str(ROOT / "services/docker-broker-rust/Cargo.toml"),
                "--",
                mode,
                "nautionette/pi-agent-default:dev",
            ],
            check=True,
            stdout=subprocess.DEVNULL if mode == "prepare-test-artifacts" else None,
        )


if __name__ == "__main__":
    main()
