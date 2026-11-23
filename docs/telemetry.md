# OpenTelemetry

Denkzeug exports standard OTLP/HTTP traces, metrics and correlated logs. There is
**no bundled observability database or vendor-specific backend**. Use an existing
SigNoz, Grafana/Tempo, Honeycomb or other compatible OTLP receiver.

## Enable the optional relay

Set these deployment variables (keep authorization in your deployment secrets):

```dotenv
COMPOSE_PROFILES=observability
OTEL_SDK_DISABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_COLLECTOR_UPSTREAM_ENDPOINT=https://your-otlp-receiver.example
OTEL_COLLECTOR_UPSTREAM_AUTHORIZATION=Bearer <ingest-token>
APP_ENVIRONMENT=staging
APP_VERSION=<release-or-git-sha>
```

Then recreate the stack. The relay accepts HTTP on 4318 (and gRPC on 4317) only on
private Docker networks; it publishes no host ports. Its upstream uses HTTP/protobuf,
appending `/v1/traces`, `/v1/metrics` and `/v1/logs` to the upstream base URL. If your
receiver uses Basic authentication, set the authorization value to `Basic <base64>`.
An empty upstream deliberately points to an unused loopback port, not back to itself.

The default application configuration is disabled. A backend outage does not block
normal requests: Python exporters have bounded background queues and timeouts;
short-lived agents drain a bounded queue for at most three seconds before exiting.

The relay keeps the upstream credential out of dynamically launched agent containers.
You can bypass it by pointing `OTEL_EXPORTER_OTLP_ENDPOINT` directly at an OTLP/HTTP
receiver and supplying standard URL-encoded `OTEL_EXPORTER_OTLP_HEADERS` (for example
`Authorization=Bearer%20...`). Only use an endpoint that also accepts OTLP JSON for
agent spans. A normal OpenTelemetry Collector accepts both JSON and protobuf.
For gRPC-only/vendor-specific transports, keep the relay and change its exporter.

## Coverage and labels

- FastAPI inbound HTTP and HTTPX outbound requests: backend, broker, workflow MCP
  and workers, with W3C context propagation.
- Temporal workflow submissions, executions and activities, using the official
  Temporal tracing interceptor.
- Short-lived agents: `agent.run`, `gen_ai.chat` and `tool.call`, including model,
  input/output token counts, tool name, duration and failure status.
- Agentgateway: upstream request spans with parent context from the agent. Tracing
  is generated only when explicitly enabled; its persisted routing DB is untouched.
- HTTP latency/count metrics and severity/source-only log records from Python services.

Resources include `service.name`, `service.namespace=nautionette`, `service.version`,
`deployment.environment` and `deployment.environment.name`. Keep staging and production
labels distinct even when sending both to a single receiver.

The trusted-gateway fetch wrapper forwards `traceparent` only to the configured
agentgateway origin, never arbitrary external sites. It does not forward baggage.
Tool spans describe a tool invocation, not every internal operation of third-party
MCP servers. Browser UI code does not receive ingestion credentials.

## Chat latency: compare releases with OTLP

- `denkzeug.chat.startup.duration` (histogram, `ms`): filter `stage=time_to_first_text`
  for p50/p95 of backend execution start to the first nonempty streamed text delta.
  Compare staging and production separately, group by `service.version`, and compare
  the same `mcp_selection` (`all`, `pinned`, `empty`, from turn configuration) and
  `project_count` (`none`, `one`, `multiple`). Graph sample counts with percentiles.
- Other `stage` values split out reconciliation, input and worktree preparation,
  GitHub credential issuance, broker dispatch to its `started` acknowledgment,
  readiness of the native session, Pi startup through its `commands` response, and
  model phase to first text. `time_to_first_reasoning` and `time_to_first_tool`
  are milestones from execution start, sampled only if observed before first text
  (or before the run ends without text). Stage histograms are not additive percentiles.
- `denkzeug.chat.startup.turns` counts `outcome=first_text` versus `outcome=no_text`
  in the same cohorts. Track the no-text rate beside TTFT; a failed, stopped,
  or command-only turn without streamed text must not look like a faster sample.
- `chat.startup` traces show the same stages as child spans, with the existing
  chat/turn correlation IDs and model label on the parent. Metrics never have
  chat IDs, project or tool names, prompts, payloads, or account labels.

These metrics start when the backend begins executing the turn, not at HTTP
admission or queue entry. `model_phase_to_first_text` may include hidden reasoning
or tools before prose; it is not pure provider latency. The Pi startup stage includes
extension startup and discovery, not just MCP. Use the [chat TTFT probe](spikes/project-chat-ttft.md#repeatable-full-app-benchmark)
for client-observed POST-to-SSE timing. Verify with interleaved runs of the same
model, reasoning effort, prompt, and tool/project selection; metric cohorts alone
cannot control for workload changes. Neither metric measures browser paint.

## Investigating slow startup and backend requests

1. **Start with the symptom and an exact build.** Scope SigNoz to
   `service.namespace=nautionette`, the deployment environment and `service.version`.
   For slow chat responses use the `time_to_first_text` histogram, not the unfiltered
   `chat.startup` span duration (no-text turns have different end semantics). For
   API slowness use `denkzeug.backend.http.response_start` (ms) grouped by `http.route`
   and method. It ends at response headers. Standard HTTP duration still includes
   the open lifetime of SSE/NDJSON streams; exclude those routes when ranking full
   request latencies. Response-start does not measure body downloads or client paint.
2. **Open an individual trace.** Under `POST /agent/run`, compare `broker.startup.*`
   spans: `image_ready`, `prepare` (claims and mount configuration), `create`,
   `stage_job` (file transfers), and `start` (including the start lock). Both brokers
   use the same names and the `denkzeug.broker.implementation` trace attribute.
   After that, `agent.bootstrap.*` covers workspace files, project worktrees,
   packages, and native session preparation. A gap between broker start and agent
   bootstrap can still include Docker/process startup and stream delivery.
3. **Separate Pi, discovery and model work.** `agent.pi.startup` runs from spawn
   until commands are available (or the first model turn in one-shot mode).
   Its `agent.pi.mcp_connect`, `agent.pi.mcp_list`, `agent.pi.mcp_oauth_catalog`, and
   `agent.pi.model_api` children show known extension waits; other startup time
   includes Pi initialization, other extensions, and command delivery. Empty MCP
   selection should have no discovery spans. `gen_ai.chat` starts at Pi's model
   turn, not the first assistant block; it measures the whole model turn. Use the
   backend first-text milestone for startup latency. `chat.admission.model_catalog`
   isolates model lookup before the turn is accepted.
4. **Check storage and the event loop.** `denkzeug.sqlite.lock_wait` measures wait
   for the backend process's shared Python lock. `denkzeug.sqlite.operation.duration`
   measures time holding it (including read fetch, write commit, or transaction
   callbacks as applicable). High lock wait suggests in-process contention; high
   hold time can be SQLite work, disk I/O, external-process SQLite lock waits, or
   Python in transactions. It is not a per-statement profiler. Metrics use fixed
   `operation` and `outcome` labels. Traced operations over 25 ms or errors get
   `sqlite.*` spans with wait time, never SQL, parameters, or rows. Compare with
   `denkzeug.backend.event_loop.lag`; simultaneous spikes are a lead, not proof
   SQLite caused the stall. `first_text_persist` measures text receipt through its
   durable write separately from time to first text.

Keep p50/p95 and sample counts together. For a broker A/B use the existing
[TTFT probe](spikes/project-chat-ttft.md#repeatable-full-app-benchmark), identical
host, agent image, model, inputs and project/tool selections, and compare warm
serial turns and concurrent starts separately. Verify stage spans, tail latency,
stop behavior and startup failures before any production switch. Parity tests do
not constitute timing evidence.

**Coverage gaps:** browser receipt/paint, queue wait, exact provider first byte,
SQLite statement plans, and host disk/CPU contention are not measured by these
spans. Check container/host saturation alongside traces, and add focused
instrumentation only where the data points. Python tests disable inherited OTLP;
export tests explicitly opt in to localhost so test traffic cannot skew release
comparisons.

## Privacy and operational limits

Python exports allowlisted attributes and exception **types**, removing exception
messages, status descriptions, request URLs/query strings and arbitrary log text.
Unmatched server request paths cannot become span names. Agent spans never contain
prompts, tool arguments/results or provider responses. The relay applies another
allowlist and strips known payload/header/exception fields before forwarding.
Free-form application logs remain in the normal container logs for manual debugging;
OTLP logs intentionally provide correlation and severity, not their full text.

These are protections against accidental capture, not a sandbox against application
code deliberately writing sensitive data into telemetry. Restrict ingestion credentials,
protect the telemetry UI with authentication, set suitable retention at the receiver,
and do not publish the private relay ports.

Collector queues are bounded and in-memory; queued telemetry can be lost on restart
or a prolonged outage. Exporting does not alter workflow results or implement incident
notifications, automatic rollbacks, or autonomous production changes.

## Coolify

Coolify may keep a saved Compose copy independently of Git. Update that copy when
adding the relay and telemetry environment mappings, preserving existing namespaces,
volumes, networks, domains and secrets. Ensure the observability profile is enabled,
or remove its `profiles` entry only in the always-on deployment copy. A Git push alone
is not sufficient if Coolify still uses an older saved Compose definition.

After deployment, make an authenticated backend request and check that its server
span and downstream spans appear at the receiver with the expected environment and
version. Test the receiver's unauthenticated ingestion rejection separately.

## Tests

```sh
uv run pytest tests/lib/test_telemetry.py tests/lib/test_telemetry_http.py tests/agentgateway/test_telemetry_config.py
node tests/agent/test_telemetry.mjs
```

The SDK test uses a local receiver to verify real traces, metrics, logs, privacy and
HTTP parent/child propagation. Agent checks cover parent IDs, payload-free export,
disabled behavior and trusted-origin-only fetch propagation. CI also builds the relay.
