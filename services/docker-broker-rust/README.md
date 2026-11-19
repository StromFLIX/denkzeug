# Rust Docker broker

Local migration candidate for the authenticated Linux broker's fixed verbs.
The Python implementation, base Compose file and deployment CI are unchanged.
**No production rollout is part of this port.**

See [PARITY.md](PARITY.md) for the frozen reference scope, deliberate safety
limits and verification boundaries. The full scenario mapping is
[`tests/parity-map.json`](tests/parity-map.json); recorded executions and the
fail-closed accounting report are in [`tests/evidence/`](tests/evidence/).

## Native tests

From this directory, with Rust 1.91 or later:

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
python tests/check_parity.py
```

The default test run leaves explicitly marked internet/Docker cases ignored.
Ignoring them is **not** a parity pass. To require all mapped executions on a
prepared disposable Linux Docker host, see `tests/run-local-conformance.sh
--docker`. Its environment checks fail if required fixture inputs are missing.
The daemon must be local and its named-volume storage accessible to the test
process; these filesystem fixtures are not for Docker Desktop or a remote host.

Fixture preparation, from the repository root:

```sh
docker build -f services/docker-broker-rust/tests/Dockerfile.fixture \
  --target busybox-fixture -t denkzeug/broker-busybox-fixture .
docker build -f services/docker-broker-rust/tests/Dockerfile.fixture \
  --target node-fixture -t denkzeug/broker-node-fixture .
docker build -f services/docker-broker-rust/tests/Dockerfile.pi-fixture \
  --build-arg PI_BASE_IMAGE=nautionette/pi-base:dev -t denkzeug/broker-pi-steer .
python services/docker-broker-rust/tests/prepare_gridcraft_fixture.py \
  /absolute/path/to/empty/gridcraft-fixture
```

Use the pinned production Pi base (Pi CLI 1.5.2) for
`BROKER_PI_FIXTURE_IMAGE`, `BROKER_PACKAGE_FIXTURE_IMAGE` and
`BROKER_ARTCRAFT_VERIFY_IMAGE`; use the steering image for
`BROKER_PI_STEER_FIXTURE_IMAGE`. Other inputs are described in the conformance
script. Package testing installs an explicitly chosen pinned public package
with lifecycle scripts disabled and never loads its extensions.

## No host Docker socket available

`tests/vm_conformance.py` provisions an isolated QEMU guest with Docker 26.1.4.
It needs no root, KVM or host Docker socket. It is intended for Debian x86-64
coding sandboxes with the pinned Node/Pi SDK already available. It downloads
public tooling into a marked disposable scratch directory outside the repo.
No host ports are forwarded. Only `--mode package` enables guest internet access;
other modes run offline after preparing their fixed fixtures on the host.

```sh
python tests/vm_conformance.py --workdir /tmp/broker-conformance --mode wire
python tests/vm_conformance.py --workdir /tmp/broker-conformance --mode pi
python tests/vm_conformance.py --workdir /tmp/broker-conformance --mode registry
python tests/vm_conformance.py --workdir /tmp/broker-conformance --mode artcraft
python tests/vm_conformance.py --workdir /tmp/broker-conformance --mode package
```

Run modes sequentially in one work directory; they reuse tooling but replace the
guest disk. The guest shutdown marker is checked, not just QEMU's exit status.
The 1-CPU emulated runs are correctness checks, not latency benchmarks.

## Scratch packaging

The default image target is a non-root, socket-free fingerprint smoke:

```sh
docker build -f services/docker-broker-rust/Dockerfile.spike \
  -t denkzeug/broker-rust-smoke .
docker run --rm --network=none denkzeug/broker-rust-smoke
```

The explicit `candidate` target packages the static runtime, build contexts,
canonical fingerprints, volume seed permissions and an in-binary health check.
It needs no Python, shell, libc shared libraries, curl or separate probe binary.
Optional SSH/credential-helper configurations still need their own executables.

For a disposable Compose comparison only, merge `docker-compose.yaml` with
`services/docker-broker-rust/compose.local.yaml`, using a separate Compose
project, `STACK_NAMESPACE`, networks and volumes. **Never run two reconcilers
against the same live stack.** The override uses `/broker healthcheck` and an
explicit authenticated container-network bind; it does not change the base file.

To test the final runtime stages from a locally compiled static ELF in the VM:

```sh
BROKER_STATIC_BINARY=/absolute/path/to/current/static/broker \
  python tests/vm_conformance.py --workdir /tmp/broker-conformance --mode scratch
```

This mode uses the actual candidate/smoke stages with the compiled ELF supplied
in place of the networked compiler stage. It checks fingerprint output, absence
of a shell, named-volume UID/GID/mode seeding, and a live broker whose in-binary
health probe correctly fails when no Docker socket is mounted. It also records
the final image's unpacked byte size. This is not a full application staging run.

## Local service

With no arguments the binary is a loopback-only read-only probe. The explicit
`--local-runtime` switch enables all fixed verbs and reconcilers. It requires a
configured internal token and an explicitly selected disposable daemon:

```sh
INTERNAL_TOKEN=synthetic-local-token \
BROKER_DOCKER_SOCKET=/absolute/path/to/disposable/docker.sock \
BROKER_DOCKER_SELF=disposable-stack-broker \
AGENT_IMAGES_DIR="$PWD/images" \
BROKER_IMAGE_FINGERPRINTS="$PWD/images/source-fingerprints.json" \
  services/docker-broker-rust/target/release/denkzeug-docker-broker-spike --local-runtime
```

Run that command from the repo root, setting the target stack's existing network,
worker image and volume environment values. Project/chat tests also need the
named-volume paths at `PROJECTS_DIR` and `CHAT_SESSIONS_DIR` on the broker's own
Docker container identity. The runtime verifies these before mounting subpaths.

`BROKER_PORT` defaults to 19100; `BROKER_BIND_ADDR` defaults to loopback. A
non-loopback bind requires the explicit authenticated runtime.
`BROKER_DOCKER_TIMEOUT_SECONDS` defaults to 60 in runtime mode; the health ping
has a separate short deadline. `healthcheck` succeeds only when images, creative
artifacts, worker probes and the Temporal maintenance fence are all ready.
