#!/usr/bin/env bash
# Use --docker only with an explicitly selected, disposable same-host daemon.
# No production stack, image tag or Compose project may be shared with the run.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
crate="$repo/services/docker-broker-rust/Cargo.toml"
cd "$repo"
if [[ $# -gt 1 || ( $# -eq 1 && $1 != --docker ) ]]; then
  printf '%s\n' 'usage: run-local-conformance.sh [--docker]' >&2
  exit 2
fi

evidence=${BROKER_EVIDENCE_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/denkzeug-broker-evidence.XXXXXX")}
mkdir -p "$evidence"
evidence=$(cd "$evidence" && pwd)
if [[ -n $(find "$evidence" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
  printf '%s\n' 'Use a fresh, empty evidence directory; previous results are not overwritten.' >&2
  exit 2
fi
cargo test --locked --manifest-path "$crate" -- --test-threads=1 2>&1 | tee "$evidence/native.log"
cargo clippy --locked --manifest-path "$crate" --all-targets -- -D warnings 2>&1 | tee "$evidence/clippy.log"
python services/docker-broker-rust/tests/check_parity.py

if [[ $# -eq 0 ]]; then
  printf 'Native checks passed. Use --docker for the required live scenarios. Logs: %s\n' "$evidence"
  exit 0
fi

: "${BROKER_DOCKER_SOCKET:?Set an explicit disposable Docker socket path}"
: "${BROKER_FIXTURE_IMAGE:?Build the BusyBox fixture}"
: "${BROKER_NODE_FIXTURE_IMAGE:?Build the offline Node/Git fixture}"
: "${BROKER_PI_FIXTURE_IMAGE:?Build the pinned production Pi base}"
: "${BROKER_PI_STEER_FIXTURE_IMAGE:?Build the original Pi steering fixture}"
: "${BROKER_ARTCRAFT_ARTIFACT_PATH:?Prepare the pinned GridCraft artifact}"
: "${BROKER_ARTCRAFT_VERIFY_IMAGE:?Set the production Pi base with the offline verifier}"
: "${NAUTIONETTE_PACKAGE_TEST_SOURCE:?Choose a pinned public package for the package scenario}"
export BROKER_PACKAGE_FIXTURE_IMAGE=${BROKER_PACKAGE_FIXTURE_IMAGE:-$BROKER_PI_FIXTURE_IMAGE}
[[ "$BROKER_DOCKER_SOCKET" = /* && -S "$BROKER_DOCKER_SOCKET" ]] || {
  printf '%s\n' 'Docker must be an explicit, local Unix socket' >&2
  exit 2
}
export DOCKER_HOST="unix://$BROKER_DOCKER_SOCKET"
docker version --format '{{.Server.Version}}'
cargo test --locked --manifest-path "$crate" --lib real_gridcraft_archive -- --ignored 2>&1 | tee "$evidence/download.log"
# Compile as the current user. On an ephemeral CI host only, use sudo for
# the test binaries that need access to daemon-owned volumes and trust files.
# Keep the compiler/cache and the reference Python/Node suites unprivileged.
for suite in docker_registry docker_inventory docker_package; do
  if [[ ${BROKER_CONFORMANCE_SUDO:-0} == 1 ]]; then
    binary=$(cargo test --locked --manifest-path "$crate" --test "$suite" --no-run --message-format=json |
      python -c 'import json,sys; [print(r["executable"]) for line in sys.stdin if (r := json.loads(line)).get("executable") and r["target"]["name"] == sys.argv[1]]' "$suite")
    [[ -x $binary ]]
    sudo --preserve-env=BROKER_DOCKER_SOCKET,BROKER_FIXTURE_IMAGE,BROKER_NODE_FIXTURE_IMAGE,BROKER_PI_FIXTURE_IMAGE,BROKER_PI_STEER_FIXTURE_IMAGE,BROKER_ARTCRAFT_ARTIFACT_PATH,BROKER_ARTCRAFT_VERIFY_IMAGE,BROKER_PACKAGE_FIXTURE_IMAGE,NAUTIONETTE_PACKAGE_TEST_SOURCE \
      -- "$binary" --ignored --nocapture --test-threads=1 2>&1 | tee "$evidence/$suite.log"
  else
    cargo test --locked --manifest-path "$crate" --test "$suite" -- --ignored --nocapture --test-threads=1 2>&1 | tee "$evidence/$suite.log"
  fi
done

# Shared JS/helper/Compose invariants are not reimplemented in a different
# language. The reference's nine opt-in Docker cases have Rust counterparts;
# running Python's SDK mocks is not evidence that the Rust paths executed.
[[ $(pi --version) = 1.5.2 ]] || {
  printf '%s\n' 'Reference Pi tests require Pi CLI 1.5.2' >&2
  exit 2
}
PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 node --test \
  tests/agent/test_chat_control.mjs tests/agent/test_chat_recovery.mjs tests/agent/test_chat_recovery_rpc.mjs 2>&1 | tee "$evidence/reference-node.log"
NAUTIONETTE_DOCKER_TESTS=0 uv run --locked pytest tests/broker --junitxml="$evidence/reference.xml" 2>&1 | tee "$evidence/reference.log"
python services/docker-broker-rust/tests/check_parity.py --evidence \
  --rust-log "$evidence/native.log" --rust-log "$evidence/download.log" \
  --rust-log "$evidence/docker_registry.log" --rust-log "$evidence/docker_inventory.log" \
  --rust-log "$evidence/docker_package.log" --python-junit "$evidence/reference.xml" \
  --report "$evidence/report.json"
printf 'Broker conformance gate passed. Evidence: %s\n' "$evidence"
