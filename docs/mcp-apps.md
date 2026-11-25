# Interactive chat results (MCP Apps)

Denkzeug has one MCP Apps host for workflow results and MCP tool results. Apps
are installation data, managed privately by their owner under **Settings → Apps**,
not code that users must commit to Denkzeug. There is no marketplace or main-nav
app destination. The retired miniapp feature is not used or restored.

## Deployment: a separate sandbox origin is mandatory

The Compose `app-sandbox` service serves only a host-controlled proxy. It has no
API, credentials, database, application dependencies or writable data volumes.
Its sole listener is port 8092. Publish it on a separate HTTPS origin, for example:

```dotenv
MCP_APPS_SANDBOX_URL=https://sandbox.example.net
MCP_APPS_HOST_ORIGINS=https://denkzeug.example.net
```

`MCP_APPS_HOST_ORIGINS` is an exact comma-separated list of allowed **embedding**
web origins. It defaults to `DENKZEUG_PUBLIC_URL`/`APP_URL` in Compose. Include every
actual chat origin used by browsers, including any staging or legacy aliases.
The exact bundled client origins are included separately by default:
`capacitor://localhost` for desktop/iOS and `https://localhost` for Android.
Set `MCP_APPS_INCLUDE_NATIVE_ORIGINS=false` to disable that automatic inclusion;
then list any native origins you want to permit explicitly. Arbitrary custom
schemes/hosts and opaque `null` parents remain forbidden.
Configure DNS, TLS and the reverse proxy to point the sandbox origin at
`app-sandbox:8092`, **not** the backend, website, or frontend. In Coolify, add a
Compose domain for service `app-sandbox` selecting internal port 8092.

When upgrading an existing Coolify application, let it parse the updated Compose
file before assigning this new service's domain. Its API can acknowledge a domain
update while silently omitting services absent from its stored Compose definition.
After assigning the domain, redeploy the tested revision so routing labels and TLS
are regenerated. Verify the persisted hostname and a certificate-verified HTTPS
`/healthz` response; do not work around a missing certificate by disabling TLS checks.
The staging deployment smoke checks both the authenticated workflow result path
and the credential-free sandbox's HTTP CSP after this provisioning step.

The web backend injects the configured sandbox origin into the shell's frame CSP.
A same-origin, invalid, or absent configuration fails closed: results stay readable
but executable interfaces do not mount. Never reverse-proxy the sandbox endpoint
under the authenticated chat origin to work around this restriction.

Local development needs no new DNS:

```sh
MCP_APPS_HOST_ORIGINS=http://localhost:9000 python3 services/backend/nautionette_backend/app_sandbox.py
# Backend environment:
# MCP_APPS_SANDBOX_URL=http://localhost:8092
```

Use the exact hostname of the frontend (localhost and 127.0.0.1 are different
origins). HTTP is accepted only for loopback development. Different ports provide
origin isolation, **not cookie isolation**; a dedicated hostname/site is preferable
in production. Host authentication cookies remain HttpOnly, and the sandbox must
never receive application credentials or host application storage.

### Desktop and mobile clients

The host preserves the bundled origin explicitly instead of assuming every URL
implementation can serialize a custom scheme with `URL.origin`. Parent origins
are derived from the app page, not from its configured remote API server URL.
Capacitor hostname and iOS/Android schemes are pinned in `capacitor.config.json`.
Custom-scheme support is allowed only for embedding hosts—never as a sandbox
serving URL or app resource/connect CSP domain.

These are embedding permissions, **not authentication**: private snapshots still
require the acting account, and the proxy has no API, token or account authority.
The outer iframe stays on a different origin; the inner app remains opaque and
has no Node, preload, account-store or OAuth bridge access. Message forwarding
still requires the exact source window and origin. Do not allow `parent=null`,
wildcard ancestors, disable web security or grant Node integration as a workaround.

Upgrade/redeploy the `app-sandbox` service for the new host-origin support. Current
Electron builds which already serialize `capacitor://localhost` correctly can be
unblocked by this server update; other URL implementations may require the rebuilt
client. Rebuild/install native clients to pick up client-side origin handling and
platform metadata changes—updating the web server cannot replace installed assets.

The native smoke uses the real Electron application, registered scheme, isolated
preload and sandbox service to check SDK input/results, clipboard copy, safe links
and denied Node/IPC access. A separate Chromium app-asset session checks Android's
exact `https://localhost` origin. This is not a physical Android/iOS device test;
WKWebView, mobile clipboard/popups and device lifecycle still need device acceptance.

## Author and version an app

An app revision contains a self-contained HTML document, title, description,
Draft 2020-12 data schema, sample data, change note, timestamp and HTML SHA-256.
Scripts/styles must be bundled inline. No npm builds or user code execute on the
backend. Build trusted source in an agent's workspace or your own development
checkout and publish the resulting HTML.

Use the official app SDK, pinned with a lockfile:

```js
import { App } from '@modelcontextprotocol/ext-apps'

const app = new App({ name: 'Report', version: '1' })
app.ontoolresult = result => {
  document.querySelector('#count').textContent = String(result.structuredContent.count)
}
await app.connect()
```

Register handlers **before** `connect()`. Bundle this JavaScript and its dependencies
into the HTML (for example with Vite and `vite-plugin-singlefile`). Use safe DOM
APIs; sanitize Markdown HTML before rendering it. Do not fetch scripts from a CDN
for an app in the local library: local apps have a network-denying resource policy.

Settings offers create/import, source/schema/sample editing, sandboxed preview,
revision history, restoration as a **new** revision, export, and archive. Editing
is an unsaved form draft until explicitly published. Existing history is immutable.
Archiving blocks new workflow references; it never deletes old results.

The MCP tools `backend_list_apps`, `backend_publish_app`, `backend_get_app`,
`backend_get_app_revision`, and `backend_archive_app` expose the same owner checks
and optimistic concurrency as the UI. Publish with `expected_revision: 0` for a
new slug, or the current revision when updating. In the default coding agent,
`backend_publish_app` additionally accepts `body.html_path` instead of `body.html`:
this reads a local UTF-8 bundle without passing hundreds of kilobytes through model
context. The service receives and validates the actual HTML, not a filesystem path.
Member agents cannot use this local-filesystem convenience.

**Trust model:** publishing authorizes that revision's code to receive your
workflow data, just as deploying a workflow authorizes its code. Import only
software you trust; preview receives only the sample data you choose. This is
not a marketplace that automatically executes unreviewed packages. Remote apps
receive only the arguments/result already supplied to or returned by their
selected MCP server, never unrelated conversation context or host credentials.

Source and revision history live in the existing backend SQLite data volume.
Back up that volume as normal; export packages are portable and credential-free.
Git provenance/synchronization and multi-file server builds are not implemented.
Users can maintain source in their own Git repo and import its bundled output;
Denkzeug does not require or silently follow a Git branch.

Limits per owner are currently 100 apps, 100 revisions/app, 32 MiB of revision
content, and 1 MiB per HTML/data payload (schemas: 64 KiB). Workflow and remote
MCP snapshots each have a 64 MiB/1000-instance limit. Quota errors preserve the
original readable result. There is deliberately no destructive cleanup API yet.

## Return an app from a workflow

```python
from nautionette.mcp_apps import app_result

return app_result(
    "daily-report",
    {"count": 12},
    summary="12 findings are ready for review.",
    revision=1,
)
```

Equivalent JSON result:

```json
{
  "summary": "12 findings are ready for review.",
  "data": {"count": 12},
  "ui": {"app": "daily-report", "revision": 1}
}
```

Declare these fields in the workflow's output schema. The app revision's
`input_schema` validates `data` (the value delivered as `structuredContent`).
The schema can use local fragment `$ref`s only; the server never retrieves remote
schema identifiers. Omit `revision` to select the active revision at completion.
The backend resolves under the **run owner's** account and validates data in a
killable, CPU/memory/wall-time-bounded worker outside its event loop and SQLite
write transaction. The selected immutable revision is then pinned atomically
with the result/outbox. Schema regexes and combinatorial validation never execute
unbounded on the shared backend thread.

Delivery attaches a saved app-instance reference to chat metadata and uses
`summary` as readable message text. Chat and run details show the app first,
with the Markdown summary under **Original result** instead of duplicating it
outside the card. This fallback is collapsed by default and opens automatically
if the interface fails to load. Opening a chat or retrying interface loading
never repeats a workflow. Outbox retries cannot choose a newer UI revision.
A missing/archived app or invalid envelope/data produces a safe diagnostic and
text fallback without changing successful run status or blocking delivery.
`notify: false` is still silent. Existing string/Markdown/JSON results are unchanged.
Historical messages are not automatically rewritten.

Interfaces lazy-load on their first approach to the viewport. After that, their
iframes stay mounted for as long as the message stays mounted in the chat;
scrolling away and back does not refetch the snapshot, reconnect the app, or
reset local filters and other interface state. Leaving the chat still tears down
the bridge and aborts pending requests. Reopening the chat starts a fresh view of
the same immutable result, without rerunning its workflow or tool.

The host gives the embedding iframe, sandbox proxy and opaque app document an
explicit theme-matched backing surface before app scripts run. This avoids white
iframe tiles during dark-mode loading/repainting. Host-context updates repaint
these defaults in place without navigating frames or resetting filters. App
styles can still override the document's default background and text colors.
Only the light/dark scheme and validated hex colors travel in the sandbox URL;
result data and account credentials never do. This needs both the updated sandbox
service and frontend (rebuild installed native clients to update bundled assets).

Keep a useful summary: it is the fallback for unavailable sandboxes and also the
text visible to the model. On the execution path, App HTML and app-private MCP result `_meta` do not enter
model context or model-retrievable native session details. A one-shot container
file carries the snapshot to the supervisor and is consumed immediately; history
reads project away out-of-band tool details before searching or pagination.
Explicit app-source inspection tools remain intentional source reads. The original run result remains available in run history.

## Connected third-party MCP Apps

The agent advertises `io.modelcontextprotocol/ui`, detects `_meta.ui.resourceUri`
(and the legacy `_meta["ui/resourceUri"]`), and reads the resource through the
existing MCP session. The pinned gateway qualifies these URIs by originating
server; Denkzeug preserves that exact URI, never guesses a target from a tool name.
Only `text/html;profile=mcp-app` content is accepted, as text or UTF-8 base64 blobs.

A successful chat tool call saves the HTML, original arguments, full UI result,
resource policy, origin URI and digest independently of the ephemeral agent.
Its timeline event contains only the instance reference and normal textual result.
App-only tools are excluded from the model's tool list. Failure to load/save an
interface does not turn a completed business tool into a retryable failure.
Remote app resources are snapshots attached to tool results, not silently installed
into the user's app library. No business tool is rerun to reopen a saved interface.

## Supported capabilities and security

This first host supports initialization, tool input/result delivery, inline sizing,
theme context, bounded teardown, and HTTPS open-link requests. It uses the official
`@modelcontextprotocol/ext-apps` AppBridge. Local workflow apps and remote MCP Apps
use exactly the same component/bridge and standard protocol.

A browser iframe is **not an OS-level network sandbox**. CSP governs ordinary
fetch/WebSocket/resource loads, not every browser networking feature. The host
also disables peer-connection constructors before authored scripts, denies nested
frame/worker realms, and disables DNS prefetch as defense in depth. These guards
are not a promise of browser-independent air-gapping: published local code must
be trusted by its owner to receive the supplied data.

The outer proxy has a different origin, with `allow-scripts allow-same-origin`.
The inner app has an opaque origin (`allow-scripts` only); it cannot read the
proxy DOM, host DOM, cookies, or local storage. Resource CSP is enforced through
HTTP headers on the proxy and inherited by the inner document, not a removable
meta tag. Network/static resource origins must be explicit HTTPS origins; directive
injection, wildcard origins, credential URLs, unsafe-eval, forms, plugins, base URL
rewriting, nested external frames and top-level navigation are not granted.
Messages are bound to exact frames/origins, size/rate bounded, and sandbox-control
notifications cannot be forged by the inner view.

The host does **not** advertise or forward server tool calls, resource browsing,
chat messages, model-context updates, sampling, or backend mutations from views.
Apps requiring those capabilities may render partially or report unsupported
operations; this is not full compatibility with every third-party app.
External link requests allow HTTPS without URL credentials. Where user activation
or browser popup policy prevents opening, the host displays a normal clickable
link instead. No arbitrary protocols or local file paths are opened.

There is no standard MCP Apps clipboard RPC. Clipboard permission is not granted
in this first version. Apps may provide a user-triggered selectable-text/legacy
copy fallback; browser behavior varies. The chat's existing Copy response control
remains available. Never add a custom mandatory bridge method that makes otherwise
standard app interfaces depend on Denkzeug.

## Tests

- Backend: private ownership, scoped keys/delegation, immutable revisions, quotas,
  schema validation, completion-time pinning, exactly-once outbox, diagnostics,
  remote snapshots and no-op preservation.
- Browser: a **real official App SDK client** receives input/results through the
  shared host; sandbox CSP blocks network and parent access; link and unsupported
  tool behavior are checked. Settings tests cover publish/history/import/export.
- Agent: discovery capabilities, visibility, private metadata exclusion, exact
  resource routing, UTF-8 blob decoding and resource bounds.

Run `uv run pytest tests/backend/test_local_apps.py tests/backend/test_mcp_app_results.py tests/lib/test_mcp_apps.py`,
`node --test tests/agent/test_mcp_apps.mjs`, and frontend `npm test` /
`npx playwright test tests/mcp-apps.spec.js tests/apps.spec.js --workers=1`.
