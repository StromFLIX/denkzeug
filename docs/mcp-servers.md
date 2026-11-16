# MCP servers: HTTP and stdio

Under **Settings > MCP servers**, add a server and choose its transport:

- **HTTP** connects to an existing streamable HTTP endpoint. Both backend and
  agentgateway must be able to reach it. The optional access token authenticates
  the connection to that MCP server; `$VARIABLE` references a gateway environment
  variable, as before.
- **stdio** runs a command **inside the agentgateway container**. There is no URL
  or port to publish, and no separate service to start. agentgateway launches the
  process for MCP client sessions and exchanges JSON-RPC over stdin/stdout.
  It is not a general-purpose background-service supervisor.

Names become tool prefixes, so choose stable names. File-owned baseline targets
are visible but cannot be edited or removed through Settings.

## Deploy the runtime and settings support

Rebuild/redeploy **agentgateway, backend and frontend-web** together:

```sh
docker compose up -d --build agentgateway backend frontend-web
```

In Coolify, redeploy the updated Compose stack. The gateway version is controlled
by the tested pin in `services/agentgateway/Dockerfile` (currently v1.7.0), with the
upstream binary copied into a Node 24 / Debian Trixie image.
It includes Node, npm, npx, Python 3, uv and uvx. Commands requiring other binaries,
browsers, native build tools, private package-registry configuration or system
libraries need those installed/configured in a custom image first.

**No deployment-level gateway version override is needed.** Compose deliberately
does not forward `AGENTGATEWAY_VERSION` as a build argument. Coolify saves variables
in Compose defaults as persistent settings, which can leave an old version pinned
across later releases. Remove legacy `AGENTGATEWAY_VERSION` entries from normal and
preview environments; they no longer control this Compose build. Repository pin
updates go through CI and the normal release pipeline, not an untested `latest` tag.
A custom standalone Docker build can still explicitly choose a build argument;
pre-initialization target conditions require v1.7.0 or a compatible newer release.

The gateway health component reports selection-policy coverage and becomes
`degraded` if named runtime targets in a multi-target gateway are still unscoped.
Inspect backend bootstrap logs for a sanitized phase/HTTP status instead of
assuming filtering is active.

The gateway remains non-root (UID/GID 65532). Its SQLite config and package caches
live in the existing `agentgateway-data` volume. Stdio processes get a minimal
`PATH=/usr/local/bin:/usr/bin:/bin` and `HOME=/data/mcp`, plus the environment values
entered for that target. Removing those two default rows resets them to their
runtime defaults; overriding them explicitly is supported. The working directory
is `/data`; use absolute paths for scripts. No Docker socket is mounted.

## Failure isolation and health

The shared `/mcp` endpoint uses agentgateway's `failureMode: failOpen`: failed
upstreams are skipped during initialization and discovery, while healthy servers
keep their normal namespaced tools. This is **upstream availability isolation**,
not permissive authentication. Rejected credentials still reject that target's
calls; failures are not rerouted to another server, and business calls are never
replayed. If every upstream fails, discovery still returns an error. Isolated
save/OAuth probes still fail when their sole target is unavailable.

OAuth maintenance also removes owned tokens that expire while still
`gateway_pending`, including a new login or rotated token that never passed
federated readiness. Saved connection settings/tool schemas remain available for
sign-in again; tokens and single-use exchanges are not replayed. External edits
to the gateway target are not overwritten or deleted by this cleanup.

`/api/system` checks both model routes and bounded MCP initialization/tool
listing. A working `/v1/models` no longer hides a broken shared tools endpoint:
that gateway component is reported as degraded, without exposing upstream error
bodies or credentials. The native gateway readiness endpoint is only process
readiness, not proof that every configured upstream is authenticated.

Deploy both **agentgateway and backend** for these protections; restart/recreate
the gateway to load the baseline failure policy. Existing agent turns that
started without MCP tools need a new turn to rediscover them.

## MCP request deadlines and agent overrides

Ordinary agent MCP calls have a **120-second request deadline**. Initialization
and tool discovery have a separate **30-second** budget; interactive OAuth sign-in
is not charged to a business call's request deadline. The deadline stays active
through response-body consumption and is not reset by session recovery.

For a deliberately slower (or faster) call, agents can use the local utility:

```json
{
  "tool": "workiq_retrieve",
  "arguments": {"query": "Find the usage dashboard", "strategy": "copilot"},
  "preset": "long"
}
```

Call `mcp_call_with_timeout` with that input. Presets are `fast` (**30s**),
`normal` (**120s**, the default), and `long` (**600s**). Alternatively, supply
`timeout_seconds` as an integer from **1 to 600**, instead of `preset`. This is a
per-invocation override: concurrent calls keep their own budgets. The wrapper can
only reach already selected MCP tools, uses Pi's normal argument validation,
permission hooks and account/OAuth checks, and never injects a timeout argument
into a remote server's schema or arguments. It cannot call local shell/file tools
or grant additional MCP access. It is absent when no MCP tools are selected.

agentgateway v1.7 has a separate, otherwise hidden **10-second HTTP subrequest
limit**. The baseline workflow target and managed HTTP targets now default their
`policies.http.requestTimeout` to **615s**, allowing the agent's deadline to fire
first. Backend startup upgrades existing managed targets without replacing
credentials, TLS policies, custom conditions or explicit administrator timeouts.
That exact default is normalized out of credential-ownership snapshots, so it
does not invalidate existing OAuth connections. Stdio targets do not accept this
HTTP policy. Explicit administrator/provider limits and the runtime's HTTP idle
limits remain in force; these are total request deadlines, not a way to bypass
other transport safeguards. Custom file-owned HTTP targets should set this same
policy explicitly. LLM/model route timeouts are unchanged.

A client deadline now produces an actionable **"MCP tool … timed out after …s"**
error, naming the selected budget. Caller cancellation is not mislabeled as a
timeout. A 504 is described as a reported gateway/upstream timeout; an opaque 500
remains a generic transport failure, optionally with a fixed, sanitized gateway
reason such as `gateway_send_failed`. This gateway version cannot safely identify
which underlying cause produced every 500. Raw error bodies/credentials are never
shown, and unsafe gateway error-detail exposure remains disabled.

**A timeout does not prove that the upstream operation stopped or did not run.**
Neither deadlines nor 5xx failures trigger an automatic tool replay, including for
read-only tools. Inspect the outcome before retrying a mutation. Deploy the
backend, gateway baseline and rebuilt default agent image together; new turns
load the updated utility and error descriptions.

## Agent startup discovery and direct tool availability

Pi still discovers live tools on every agent start. For an explicit tool selection,
the backend resolves known server prefixes against current gateway configuration
and passes an immutable `x-nautionette-mcp-targets` header through initialization,
listing, calls, recovery, fresh OAuth sessions and cleanup. Unselected external
runtime targets are excluded **before** initialization. `null` (all tools), unknown
or raw tool ownership, config outages and truly singleton gateways retain ordinary
unrestricted discovery. An empty selection is distinct from a missing header.
The exact tool allowlist still applies after discovery; this header is an
optimization, **not authorization**.

The local backend and file-owned workflow service remain available for private
first-party capabilities. Keeping the workflow baseline unfiltered also allows a
fresh gateway to boot with its single baseline target before backend registration;
no dummy upstream or name change is introduced. Backend bootstrap decorates existing
hybrid targets, preserving credentials and custom conditions as an outer AND.
Ownership comparisons ignore only this exact app-owned wrapper, so it cannot make
an OAuth refresh look like an external edit. App writes are serialized during
reconciliation and resources are re-read before updating. Gateway config PUT has
no compare-and-set: external configuration writers must still coordinate with
Settings/bootstrap. Additional file-owned targets are not rewritten and retain
their configured startup behavior.

Model capability validation no longer builds the tool picker when its cache is
cold. Model metadata uses the same 60-second TTL/invalidation but does not
initialize MCP servers merely to admit a message.

All selected MCP tools and their full schemas are directly available from the
first model request. The deferred-schema/tool-search experiment has been removed:
there is no automatic search activation, names-only directory, or schema-mode
switch. Local coding/history and first-party output/delegation/creative tools are
unchanged. This intentionally keeps the larger schema prompt to preserve the
established workflow and integration calling pattern without a loader round trip.

Chats that used the experiment retain their history. On their next fresh agent
process, a one-time migration restores today's allowed MCP tools to direct access
and removes our old built-in search activation when no other installed extension
needs deferred discovery. Other extensions' tools/search configuration are not
removed. Legacy `AGENT_MCP_SCHEMA_MODE` deployment entries no longer affect agent
behavior and can be deleted. Apply the rollback with rebuilt agent/broker images
and a new turn; already-running processes retain their original code/loadout.

The private OAuth bridge augments live discovery with saved disconnected-tool
schemas and gate metadata. It skips redundant discovery when no selected,
published hosted-OAuth target needs fresh owner registration. Unknown config or a
connection/revision change falls back to fresh discovery. Call-time gates, expiry,
demand-driven reconnect and fresh authenticated dispatch remain in force. The
agent closes its MCP session on orderly shutdown; forced termination still relies
on gateway cleanup. Sessions are not shared across chats, and mutations are never
blindly replayed.

Deploy **backend, broker and rebuilt agent images** together for these changes;
frontend changes are not required.

## Brave Search example

In **Settings > MCP servers > Add server**:

| Field | Value |
| --- | --- |
| Name | `brave-search` |
| Transport | `stdio` |
| Command | `npx` |
| Arguments | `["-y", "@brave/brave-search-mcp-server", "--transport", "stdio"]` |
| Environment variable name | `BRAVE_API_KEY` |
| Environment variable value | Your Brave Search API key |

Pin the npm package to a version you have reviewed for repeatable deployments:
replace the package argument with `@brave/brave-search-mcp-server@<version>`.
Use `--transport stdio`, **not** the `--transport http` in some upstream client
examples. The command must speak MCP on stdout; diagnostics belong on stderr.

The executable and arguments are separate, not a shell command line. An argument
containing spaces stays one argument. Stdio arguments and environment values are
literal: `$VARIABLE` is **not** expanded. Hybrid API resources receive the exact
strings you enter; doubling dollar signs would change the process's real values.
To give Brave
its API key, enter the actual key in its environment row, not in the HTTP token
field or the argument array.

Environment values are never returned to the browser or MCP/API callers. On
edit, leave a stored value blank to keep it, type a replacement to change it, or
remove its row to delete it. A new blank row value is an empty string. To replace
a stored value with an empty string, remove and re-add the row.

## Hosted HTTPS OAuth (recommended for WorkIQ)

Enter the name and endpoint URL, then click **Connect**. Authentication and
OAuth discovery are automatic. Known provider defaults (including WorkIQ) are
selected behind the scenes; there are no provider/discovery buttons or repeated
shared-account checkbox. The Connect action authorizes connecting the account
to this shared instance; provider consent still happens in the browser.

Transport, manual access tokens and provider configuration are under
**Advanced settings**. Existing approved client IDs are used first. For public
web clients, Client ID Metadata Documents are preferred when the provider advertises
support; dynamic registration remains a compatibility fallback. Native registrations
send `application_type=native`, web registrations send `application_type=web`.
If a provider forbids automatic registration, enter its approved client ID once.
No WorkIQ CLI is installed or run on the HTTPS path. The desktop return-to-app
behavior described below applies to both HTTPS and CLI callbacks.

For desktop login, the desktop binds its callback port before login begins.
For **Web callback**, the browser returns to the configured instance HTTPS URL;
web/mobile clients do not need a desktop bridge. Configure `DENKZEUG_PUBLIC_URL`
(or `APP_URL`) to the canonical public HTTPS origin and register the exact callback
`https://<instance>/api/mcp-oauth/web-callback` with the provider. Request Host and
forwarded headers never select the callback destination. Both GET/query and
`form_post` work; the callback accepts only an expiring, one-shot state capability
created by a signed-in MCP administrator. Anonymous form-body reads have a
10-second deadline and a 64 KiB limit. It does not authenticate other API routes
or publish an MCP target. Poll/cancel belong to the account that started the login.

The backend generates PKCE/state, exchanges the returned code, verifies authenticated
MCP through an isolated agentgateway route and only then publishes the HTTP target.
The server, not the desktop/browser, renews the token, so leaving a client open is
not required after login.

Generic public-client desktop providers need a registered client ID and loopback URI in
**OAuth client settings**. Discovery cannot prove a client's registered redirect
permissions; the provider checks these during sign-in. Port 0 selects an ephemeral
port only for registrations that permit it. WorkIQ's preset uses its documented
`http://localhost:12798` callback, but all MCP traffic stays on the HTTPS endpoint.
Entra public-client and templated-issuer metadata compatibility is handled
explicitly; resource indicators are used for other providers.

### Generic provider options

| Setting | Supported options |
| --- | --- |
| Grant | Authorization code + PKCE S256; device authorization; client credentials |
| Token endpoint client authentication | `none`, `client_secret_basic`, `client_secret_post`, `private_key_jwt`, `client_secret_jwt` |
| Authorization-code callback | Desktop loopback or canonical instance HTTPS; query or form POST |
| Client registration | Approved/manual client ID, DCR (public or secret-authenticated clients, optional initial access token), public web Client ID Metadata Document |
| Metadata | Standards discovery, or explicit validated issuer/endpoints for non-discoverable providers |
| Resource targeting | RFC resource indicator, provider-specific `audience`, or neither; optional explicit value |
| Authorization parameters | Allowlisted `prompt`, `login_hint`, `domain_hint`, `access_type`, `audience`, `organization` |

**Device authorization** opens the provider's verification page and displays the
user code. The backend retains the device code and enforces the provider polling
interval, including `slow_down`; a lost token result is never automatically replayed.
The provider must advertise the device endpoint (or it must be entered in manual
metadata mode). This does not make device-code-only CLIs compatible with the
separate CLI callback bridge.

**Client credentials** requires a pre-registered confidential client and MCP
application permissions. It does not involve a browser or require refresh tokens:
maintenance obtains a new access token and checks authenticated gateway readiness.
Providers may reject application permissions even when user-delegated login works.

Client secrets, private signing keys and optional DCR initial access tokens are
write-only. Blank fields preserve a saved credential; changing issuer, client ID
or token endpoint drops the old credential instead of sending it to the new target.
Client credentials used for device authorization are also bound to that device
endpoint, and initial registration tokens are bound to their registration endpoint.
Bindings are encrypted with the credentials and checked again immediately before use.
Advanced API callers can use `clear_client_credentials: true` during preparation.
Signing clients must pre-register their public key with the provider. Supported
private keys are RSA (2048+ bits), P-256/P-384/P-521 and EdDSA; JWT algorithms must
match both key and advertised provider capabilities. Assertions use a fresh `jti`
and 60-second expiry. HMAC assertions require a sufficiently long client secret.

Manual metadata does not disable SSRF protection: issuer and network endpoints
must remain public HTTPS, requests are DNS-pinned, TLS-verified, bounded and never
follow redirects. The browser callback may only be the canonical instance callback
or a validated desktop loopback URI. Token responses must contain valid bearer
credentials and a bounded expiry. Providers omitting `expires_in` can use the
explicit `token_expiry_seconds` preparation field (1–31536000) based on their
documented lifetime; no expiry is guessed automatically.

Connections are still **shared instance accounts**, not per-user credentials.
DPoP/mTLS sender-constrained tokens, native-broker/device-bound login and token
exchange are not implemented. Implicit and password grants are intentionally
unsupported. Scope changes can be configured and consented through Advanced
settings; automatic tool-challenge scope escalation is not yet supported. These
options broaden standards-based provider compatibility, not a guarantee that any
provider or tenant policy will accept a connection.

OAuth credentials are encrypted in the backend DB with an auto-created mode-0600
`mcp-oauth.key` in the backend data volume. Back up that whole volume, not only
SQLite. Access tokens are also held by agentgateway for authenticated MCP
requests; neither token nor PKCE verifier is returned to browser/API/agent tools.
Missing/invalid backup keys are reported, never silently replaced over existing
credentials. Refresh tokens are fenced before use; rotated credentials are
sealed before gateway updates, and uncertain refresh outcomes require a new
login rather than replaying an old token.

Expired/revoked connections are removed from federation to avoid breaking other
MCP tools. Expiry alone never interrupts the user. Saved tool schemas remain
available so an agent can request a disconnected server's tools. Only an actual
tool call creates a compact, provider-neutral **Login** dialog in the workspace.
The call is held **before dispatch**, for up to **3 minutes**, while the user signs
in with the saved settings. Receiving tokens or acknowledging the gateway config
write does not release the call. The connection remains `gateway_pending` until
a fresh session on the federated MCP endpoint completes initialization and lists
the target's authenticated tools. The comparison accounts for agentgateway's
raw single-target names versus `<server>_<tool>` names in multi-target federation;
an unrelated server's tools cannot satisfy the check. Individual readiness checks
are bounded to 10 seconds, and interactive activation stops with an actionable
error after a fixed 60-second readiness budget rather than spinning indefinitely.
Readiness polls reuse the validated token and saved probe catalog. This also
applies to refresh and lost-write recovery. The agent then dispatches through a separate fresh MCP session, never
the pre-login session's cached upstream connection, and closes it afterwards. **Skip**, dismiss, **Cancel** or
**Snooze** fails the held call without executing it; timeout and stopped turns
cannot resume it either. No possibly-executed mutation is automatically replayed.
Cards still provide manual **Sign in again** without opening the setup form.

The popup's **Snooze** action mutes login reminders for this instance on this
**device/app profile only**, for 1 hour, 8 hours, 24 hours or 7 days. It fails the
current held calls; new demands seen on that device during the snooze also fail
instead of opening a dialog. Other devices' reminder preferences are unaffected,
but the same failed call cannot be revived by signing in elsewhere. The choice
survives reload/restart in local storage (session-only if storage is denied).
Devices without desktop authentication default to 24 hours. Manual **Sign in
again** remains available; **Resume login reminders** clears the snooze early.
Neither resuming nor snooze expiry resurrects a failed call or prompts merely
because credentials expired. A new tool demand is required. Snooze does not
refresh credentials, grant access, hide tool errors or open a browser.

Validated issuer/client/scope/callback settings are cached separately from tokens,
so retries and relogin do not repeat discovery or reset the user's choices.
Temporary metadata outages use a known valid setup. Read-only metadata and
bounded HTTPS MCP initialization checks can retry; connection checks reuse the
same bearer token and isolated route without asking for another sign-in.
Permanent authentication/protocol errors still fail visibly. Single-use codes,
refresh tokens and uncertain dynamic
registrations are never replayed. **Remove saved connection** deletes local
OAuth credentials and the remembered setup.
Removing a live server also stops refresh and removes its gateway credential.
Revoke provider consent separately if needed. External gateway edits detach
OAuth management and are not overwritten by a background refresh.

Deploy the updated **backend and default agent image** for the readiness gate;
existing running agents retain their old transport until their turn ends. The
OAuth UI also needs the updated **frontend and desktop build**; use the existing
compatible agentgateway. Unlike CLI OAuth, HTTPS OAuth does not depend on the
private CLI helper. The staging check verifies both runtime modes, generic grant
and client-auth capabilities, canonical public client metadata and live WorkIQ
metadata discovery without signing into an account. Your actual tenant's
consent, licensing, token protection and account policies still apply.

## CLI-owned OAuth (alternative desktop callback bridge)

An updated Windows, macOS or Linux desktop app can connect OAuth-authenticated
stdio CLIs without a browser in the gateway container or a public OAuth callback.
Under **Add server → Advanced settings**, choose **stdio**, then
**Authentication → CLI OAuth**.

For WorkIQ, choose its **CLI adapter**, review/accept its linked CLI EULA and the
shared-account notice, then click **Connect**. The adapter pins
`@microsoft/workiq@1.0.1`, enables its supported browser-auth setting
(`disableBrokeredAuth=true`), selects English CLI status messages, accepts the
EULA only after that explicit checkbox, and runs `auth login` in agentgateway.
Broker-required/device-bound tenant policies are still enforced by Microsoft;
this is not a policy bypass. The CLI owns PKCE, token exchange, storage and refresh.

Updated desktop builds bring the **window that started the login** to the front
once its state-validated loopback callback is received, including restoring a
hidden/minimized window. The browser displays a credential-free receipt page.
If OS focus restrictions keep it in front, **Open Denkzeug** uses a registered
focus-only app link with a short-lived, one-shot nonce. OAuth codes, state and
tokens are never passed through that link; token exchange and the final MCP
connection result still occur through the existing authenticated bridge. Query
parameters are cleared from the receipt page's visible URL. Browsers/operating
systems may still require confirmation for app links, and uninstalled Linux
AppImages may need desktop integration for the link fallback.

These desktop-return and bundled popup UI enhancements require an updated
desktop build. They are separate from the earlier backend-only MCP probe retry.

The gateway captures the CLI's actual browser-launch URL (`BROWSER`, `xdg-open`,
or supported console output), validates its code/PKCE loopback flow and checks
that the login process owns the indicated port. The desktop binds that same port
on the user's machine **before** opening the system browser. Query GET and
`form_post` POST responses are forwarded through authenticated backend APIs to
that specific CLI listener. WorkIQ uses dynamic ports and `form_post`; the hosted
plugin's port 12798 is not assumed. No firewall port needs opening.

Generic CLIs use the same executable as their MCP server, with separately entered
**Login arguments** and optional **Setup commands** (a JSON array of argument
arrays). Only trusted commands are supported. The bridge cannot support every
OAuth app: native-broker-only, device-code-only, missing-state/PKCE, non-loopback,
or non-interceptable browser flows are rejected or time out with an actionable
failure. Web/mobile/older desktop clients are told to switch or update before
starting a login. A conflicting local callback port also fails before opening login.

New OAuth targets get `HOME=/data/mcp/oauth/<server-name>` unless explicitly
overridden; existing targets preserve their stored HOME/environment on reconnect.
The CLI account/cache is shared by all chats/users of that instance. Treat the
whole `agentgateway-data` volume and its backups as credential-bearing. Removing
the MCP target does **not** revoke provider consent or erase the CLI's account
cache; use the provider's logout/revocation mechanism when required.

Deploy **agentgateway, backend, frontend-web and the desktop client** together.
The gateway runs a private authenticated control helper on port 4001, with
`CLI_OAUTH_TOKEN` derived from `INTERNAL_TOKEN` (or `APP_TOKEN`). This port is not
published on the host. Optional custom deployments can set
`AGENTGATEWAY_CLI_OAUTH_URL` on the backend. Empty credentials do not enable an
open control helper. User-facing CLI login also requires `APP_TOKEN` to be
configured. Settings reports runtime compatibility before login; staging
promotion runs a read-only authenticated compatibility check.

The ten-minute login is temporary: cancellation, expiry and shutdown kill its
process group. Lost callback deliveries are fenced and never automatically
replayed. Successful CLI completion is followed by the normal isolated MCP probe
before saving. A WorkIQ exit code of 0 alone is **not** success: its explicit
success/failure messages are checked. Raw CLI output, callback HTML and OAuth
tokens never enter chat/agent tools or the helper's logs. Callback APIs live under
`/api/mcp-oauth`, outside the agent-facing MCP tool registry. Backend storage
contains only reconnect configuration, not tokens or PKCE verifiers.

See [the WorkIQ investigation](workiq-oauth-investigation.md) for actual runtime
evidence, security boundaries and remaining real-account acceptance tests.

## Checks, lifecycle and troubleshooting

Before saving a stdio target, the backend creates a random temporary gateway route
containing only that target. It performs an MCP initialize and tools/list there,
closes the discovery session, removes the temporary route, and only then writes
the shared target. **Test** repeats the isolated check. Startup recovery removes
abandoned probe routes from an interrupted backend. Failures do not overwrite a
working target. HTTP servers continue to be checked directly before saving.

A probe allows up to 90 seconds for startup/discovery. Initial npx/uvx downloads
need outbound network access and can be slow; a failed attempt may leave reusable
package-cache entries. Configured tools are trusted infrastructure traffic.
Retry after checking the command, runtime dependencies and network access. Review gateway logs on the
operator side for process diagnostics, treating third-party stderr as potentially
sensitive. The API deliberately does not echo raw process output or secrets.

A successful check proves MCP startup/tool discovery, **not** a working paid API
subscription. Make a real Brave search separately to verify its key and API plan.
Existing client sessions may retain old processes until they close or time out;
new sessions use the saved configuration. Removing a target removes its config,
not the shared npm/uv caches. Settings and caches survive gateway restarts.

## Trust boundary

**Only configure trusted executables/packages.** Package downloads and install
scripts execute with the gateway's permissions. Processes share its container,
network, UID, data volume and resource budget; they are not isolated from each
other or from the gateway's stored credentials. Clearing inherited environment
variables prevents accidental provider-key leakage, but is **not a sandbox**.

There is no public gateway port and no new unauthenticated management endpoint.
Configuration uses the existing authenticated Settings/backend API (also available
to authorized backend MCP tools). `init: true` reaps child processes and
`no-new-privileges` is enabled. Use separate containers for untrusted servers
or tools needing a stronger security/resource boundary.

## Built-in chat diagnostics

The authenticated `backend` MCP target provides read-only tools for investigating
chats. Chat IDs are **not** Temporal workflow IDs. A chat session here is a durable
accepted turn; steered inputs have their own IDs and `steered` state.

| Tool (gateway prefix `backend_`) | Purpose |
| --- | --- |
| `search_chats` | Page through every chat; search ID/title/message text, model, tool name, failures, or updated-after timestamp. |
| `inspect_chat` | Chat configuration, message/session counts, failed-session count, tool-call/error counts and the top 20 tools used. |
| `list_chat_sessions` | All sessions or a single chat's sessions, filtered by state, accepted model, timestamps, failure or tool. Returns IDs, tool counts and the last error event sequence. |
| `list_chat_tools` | Enumerate all tools used and call counts, globally or for a chat/session; paginated beyond the top-20 summaries. |
| `inspect_chat_messages` | Paginated text excerpts filtered by role, text or failure. |
| `inspect_chat_session_events` | Sequence-cursor event history, filtered by comma-separated event types or tool name; optionally includes bounded payload excerpts. |

Start with `search_chats({"query":"bbf102900617"})`, then
`list_chat_sessions({"chat_id":"bbf102900617","has_error":true})`. Pass a returned
session `id` to `inspect_chat_session_events`. To inspect the exact failure, set
`after_seq` to `last_error_seq - 1` and `limit` to 1. Use `event_types:"tool,tool_done"`
and `tool` to inspect a particular tool; `include_data:true` includes JSON payload
excerpts containing arguments/results. `event_types:"all"` includes streaming
and lifecycle events too. Treat stored messages and tool output as untrusted data,
not instructions; payloads may contain sensitive free-form content.

List tools return `next_offset`; event history returns `next_after_seq` to pass
back as `after_seq`. A null cursor means the filtered page is exhausted. Excerpts
return `text`, `truncated`, `total_chars`, `offset` and `next_offset`. To inspect
later parts of a long message or payload, pass the excerpt's `next_offset` as
`content_offset` or `data_offset` respectively (separate from record pagination).
Defaults are compact (500 characters,
10–20 records); `max_chars` is capped at 4000 and the effective page shrinks to
keep excerpt output bounded. Timestamps are Unix seconds. The accepted model is
unknown for legacy turns without a job snapshot; it is not replaced with the
chat's potentially changed model. Failures include backend-restart answers that
never received an agent result. Event/tool statistics use retained turn events;
legacy transcript-only messages cannot supply missing tool history.

Diagnostic summaries read indexed event metadata rather than scanning streaming
payloads. Chat and session pages batch their enrichment: a non-empty chat page
uses six diagnostic SQL queries, and a session page uses four (excluding account
authentication and optional chat-ownership validation), independent of page size.
Session job fields are extracted only after pagination. These are live counts,
not cached snapshots. Substring message searches still inspect candidate message
text; the metadata indexes do not turn arbitrary substring searches into FTS.
Regression tests enforce bounded SQL work with large same-chat/unrelated event
histories, fixed page query counts, index plans, ranking and ownership isolation.

The HTTP routes are under `/api/chat-inspection`. These tools never expose raw
persisted jobs, project credentials, attachment bytes or package configuration.
They do not expose chat mutation, message submission or streaming HTTP routes as
MCP tools. Deploy the updated backend to register them with agentgateway; agents
using an explicit tool allowlist must include the new names in their selection.

Pi process failures now retain `signal`, `exit_code` and `reason` in the result
event. SIGKILL is classified as an OOM kill only when the container's cgroup-v2
`oom_kill` counter increased; an absent/unchanged counter is not proof of OOM.
This also diagnoses Pi-only OOM kills when its supervisor survives and Docker's
container exit status alone would miss them. A terminated process is not cold-
restarted automatically: partial external actions must be checked before retrying.

## API shape

`PUT /api/mcp-servers/{name}` accepts either the existing `{url, token}` payload
(`transport` defaults to `http`), or:

```json
{
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@brave/brave-search-mcp-server", "--transport", "stdio"],
  "env": {"BRAVE_API_KEY": "YOUR_API_KEY_HERE"}
}
```

For stdio, omitting `env` retains the stored map. Supplying `env` replaces the map:
string values set/replace variables, `null` retains an existing value under that
key, and omitted keys are removed (apart from the runtime defaults above).
An unknown key with `null` is rejected. GET returns environment key names mapped
to `null`, never values. Switching transport discards the old transport's
credentials. Secret values belong in `env`, never command/args, which are returned
as editable launch metadata. A server-level MCP client JSON wrapper (`mcpServers`)
is not imported automatically; enter each server through the form or this API.
