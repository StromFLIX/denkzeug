# Docker broker: compatibility target and verification

## Scope

The compatibility target is the **authenticated Linux Docker broker's fixed-verb
contract at reference checkout `2ce96e92edfee57ee0c4b3ba84bd07a6bea86fbf`**.
The baseline `tests/broker` inventory has **146 test functions / 245 parametrized
cases**. `tests/parity-map.json` accounts for every function and freezes the
reference files by SHA-256 so future additions or changes cannot silently fall
out of the migration checklist.

The language-specific SDK mocks are replaced by native Rust tests of observable
HTTP, Engine API, filesystem and NDJSON behavior. Several Python parameter sets
are loops within one Rust test; test-function counts are not directly comparable.
The ten shared reference functions (14 parametrized cases) remain in Python:
unchanged offline helper verification, shared Compose/CI constraints, and default
resource consistency. The resource constraints additionally have native Rust
checks. The broker image itself needs **no Python interpreter**; the unchanged
Python offline installer runs inside the existing, isolated Pi helper image.

`tests/check_parity.py --evidence` refuses missing reference entries, changed
reference files, missing Rust selectors, absent passing scenario executions, or
failed logs. It does not interpret a successful compilation or ignored Docker
test as a passing execution. A mapping by itself is not a test result.

## Verification boundaries

The verification covers:

- Fixed routes, auth before administrative body parsing, 422 validation, absence
  of generic Docker execution and retired application routes.
- Source and packaged image fingerprints, hashed pull/retag/build fallback,
  pruning repair, single-flight force builds, image progress/error/deadline frames,
  and readiness gated on verified creative artifacts and workers.
- Fixed container resource limits and network policy, private job/output/package
  archives, project/session subpaths, cross-process locks, quotas and symlinks.
- Exact deployment/chat/turn ownership, delayed request fencing, image waiters,
  stop/create/cleanup races, stopped/foreign/duplicate/orphan renewal rejection,
  atomic Git rotation, and retryable redacted delivery failures.
- NDJSON log framing, stdout/stderr separation, EOF and large frames, silent
  timeouts, explicit-stop precedence, OOM versus SIGKILL and claim release after
  blocking filesystem preparation.
- Immutable package and ArtCraft install/attach, isolated helper options, install
  slot/storage bounds, failed candidate cleanup, referenced-volume protection,
  offline re-verification, and deployment-scoped stale-resource cleanup.
- Stack-scoped worker repair/restart, drain grace, probe readiness, retry without
  unbounded replacements, independent monitor steps, monitor lifetime, and the
  exact-commit Temporal completion receipt fence.
- OTLP traces, duration metrics, correlated redacted logs, W3C parentage and
  transport/API negotiation. The Docker endpoint supports local sockets,
  loopback TCP, verified TLS/client certificates and selected Docker contexts.

Real Docker fixtures include the original Pi blocked-tool provider/model with
Pi 1.5.2, persistent native sessions, the shipped Git credential helper, a pinned
public npm package, an authenticated loopback TLS registry, and the pinned
GridCraft CLI's production offline MCP verifier. Temporal verification concerns
Docker worker control and deployment receipts; a new Temporal SDK/worker engine
is **not** part of the broker rewrite.

The offline VM fixtures are infrastructure-level tests, not first-token latency
benchmarks and not a claim that every external ArtCraft release or provider is
available. They do not consume model-provider credentials.

## Deliberate compatibility limits

Functional broker-contract parity does **not** mean byte-identical Python SDK
internals, stack traces, framework errors, or telemetry metric names:

1. An empty internal token is rejected at startup. The Rust broker never adopts
   the Python development mode in which authentication can be absent.
2. Unverified remote Docker TCP/TLS contexts are rejected. SSH uses batch mode
   and strict known-host checking and requires the operator's SSH binary/config.
3. Registry helpers must exist on the broker's PATH when configured, as with
   Docker SDK credential helpers. A bare scratch image does not magically supply
   SSH clients, OS keychains or third-party helper executables.
4. Positive finite memory limits are required. Malformed, negative or zero
   limits cannot accidentally create an unlimited agent. Positive Docker SDK
   formats such as `6g`, `768m`, and `1.5GB` are accepted.
5. Privileged daemon errors are redacted; raw exception bodies are not echoed
   into health or API responses. JSON framing and daemon responses have explicit
   bounds. This can change error wording for invalid/malicious inputs, not the
   successful client protocol.
6. Native OTLP uses current HTTP semantic conventions. Backend consumers retain
   the fixed service identity, resource labels, correlation and safe parentage;
   FastAPI/ASGI-internal metric names are not emulated.

These are intentional safety/implementation differences, not unimplemented
fixed verbs. The target is not a reimplementation of the entire Docker SDK on
Windows or a general-purpose Docker proxy.

## Deployment status

The base Compose file and Python broker remain unchanged. The later staging
trial adds CI image/native checks and a generated staging-only Compose file.
That trial deliberately retains Python/sh for Coolify's trusted Temporal hooks;
it does not use the shell-free scratch target. See the service README.
`compose.local.yaml` and the explicit `candidate` Docker target remain opt-in for a
disposable comparison stack. Nothing here authorizes production rollout.

Results and the exact scenario execution inventory are recorded alongside this
file in `tests/evidence/`. Preserve the reference snapshot and rerun the gate if
any implementation or baseline test changes. A full application staging smoke
and comparative latency/RSS/image measurement remain rollout decisions after
review; they are not permission to bypass authentication or silently switch a
live broker.
