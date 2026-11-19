# Recorded local conformance results

These are completed local test runs, not production/staging deployment results.

- Reference: `2ce96e92edfee57ee0c4b3ba84bd07a6bea86fbf`.
- Frozen inventory: 146 Python test functions, 245 parametrized cases.
- Native Rust: **109 passed**; long Docker/internet cases are run separately.
- Required live Rust scenarios: **9 passed** (8 Docker scenarios and the public
  pinned-release download). None was counted as passing merely because ignored.
- Shared/reference Python baseline: **236 passed**, 9 original Docker cases
  skipped on the development host and covered by the mapped Rust Docker paths.
  Ten reference functions / 14 parameter cases remain shared unchanged checks.
- Original Node/Pi control/recovery: **78 passed**, Pi CLI 1.5.2.
- Scratch runtime-stage check: **passed** with the current static executable.
  Unpacked candidate image: **10,648,347 bytes** (about 10.2 MiB). This is not a
  compressed registry transfer size or a latency benchmark.
- Rust formatting, Clippy (`-D warnings`), Python Ruff and shell syntax checks
  passed. The compiler stage was built locally with the pinned Rust/musl
  toolchain; the Scratch check exercises the actual final Dockerfile stages
  using that already compiled ELF, rather than downloading a compiler image.

`report.json` is the fail-closed reference-to-execution accounting result, with
source fingerprints and exact passed Rust selectors. `build.json` records the
platform, binary digest and image measurement. The remaining files are the
underlying test output, retained so counts and skips are inspectable.

The Docker scenarios ran against a disposable local QEMU guest with Docker
26.1.4 and cgroup v2, without a host Docker socket or provider keys. Only the
public-package scenario enabled guest egress; the other guest scenarios were
offline with their fixed fixtures prepared on the host. The registry fixture
was loopback-only HTTPS with authentication and a guest-local test CA.

To recheck the saved accounting from the service directory:

```sh
python tests/check_parity.py --evidence \
  --rust-log tests/evidence/native.log --rust-log tests/evidence/download.log \
  --rust-log tests/evidence/docker-wire.log --rust-log tests/evidence/docker-pi.log \
  --rust-log tests/evidence/docker-artcraft.log --rust-log tests/evidence/docker-registry.log \
  --rust-log tests/evidence/docker-package.log --python-junit tests/evidence/reference.xml
```

This validates the evidence inventory, not a new execution. To rerun it, use
`tests/run-local-conformance.sh --docker` on a prepared disposable host or the
VM fixture modes described in the service README. Review [PARITY.md](../../PARITY.md)
for the exact compatibility target and deliberate safety limits before rollout.

No production service was replaced, and nothing was pushed or deployed.
