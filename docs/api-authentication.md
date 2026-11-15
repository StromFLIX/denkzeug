# Backend API authentication and scoped keys

The backend has one reviewed, fail-closed policy registry:
`services/backend/nautionette_backend/data/api-policies.json`.
Every HTTP method, hidden route, API mount and WebSocket has an explicit
credential policy and scope. `api_policy.validate_routes` checks the complete
assembled application at import/startup. CI also runs this check. Admission
rejects unclassified routes even if they were installed after startup.

## Trust boundaries

- **User sessions**: HttpOnly browser cookies with origin/CSRF checks; native
  access/refresh sessions retain their existing lifecycle. Admission errors retain
  CORS headers so packaged clients can read 401 responses and refresh sessions.
- **API keys**: opaque `dz_key_…` bearer credentials, owned by an account. They
  never become sessions or administrator bypasses.
- **Workflow links**: opaque `dz_wht_…` credentials accepted only for
  `POST /api/triggers/{name}` and bound to one workflow generation.
- **Internal services**: the existing `INTERNAL_TOKEN` transport is separate
  from user/API-key authentication. Explicit internal/capability policies
  preserve its existing internal operations; it never confers human authority.
  Live delegated account grants remain necessary for user-facing agent tools.
  This change does not distribute new per-service infrastructure secrets.
- **Protocol endpoints**: password sign-in, single-use bootstrap/recovery
  proofs, signature-verified GitHub webhooks and bound provider callbacks use
  their existing purpose-specific verifiers rather than an ambient API key.
- **Public exceptions**: the frontend shell, liveness and validated CORS
  preflights are explicitly classified. They grant no application access.

Every API-key operation requires a live account, an allowed credential type,
an explicit operation scope, any resource restriction, and the existing
ownership/workspace checks. Account suspension and resource-share removal
are checked on each request. Keys do not override private ownership even when
owned by an administrator. Long-lived authenticated streams check revocation
and expiry as well.

## Scopes

The inventory uses **42 explicit scopes in 18 domains**, including scopes for
human-only account/security operations. Only delegable scopes appropriate to
the account are shown in the key-creation catalogue.

| Domain | Actions |
| --- | --- |
| workflows | read, write, delete, trigger, schedule |
| runs | read, cancel, terminate |
| mcp | read, execute, manage |
| integrations | read, manage |
| chats | read, write, delete |
| agents | read, write |
| projects | read, write |
| miniapps | read, write, invoke |
| outputs | read, write |
| settings | read, write |
| tokens | read, manage |
| system | read, control |
| audit | read |
| accounts | read, manage |
| access | read, manage |
| packages | read, manage |
| artcraft | read, manage |
| speech | read, execute |

Scopes are written as `domain:action`. There is no wildcard scope, no implicit
inheritance between actions, and no blanket `backend:*` key.

Key creation, rotation and revocation require a signed-in human. Account
management, security-sensitive sharing changes and dictation admission also
retain their human/session-specific policies. Existing administrator and
recent-reauthentication requirements remain authoritative. Arbitrary chat agent
turns, queue execution and chat-to-workflow promotion are user/live-delegation
only: a metadata key must not become an owner-admin agent capability. External
keys can trigger existing, explicitly configured workflows, but cannot use a
chat prompt to obtain unrelated backend permissions. An API key with
`tokens:read` may inspect only its owner's key metadata/history; it cannot mint
another key or read another account's history.

Resource grants are explicit:

```json
{
  "scope": "workflows:trigger",
  "resources": ["immutable-workflow-generation-id"]
}
```

`resources: null` explicitly grants all resources accessible to the account,
including resources it gains access to later. Restricted keys cannot access an
unfiltered collection endpoint. Use resource-specific endpoints; the backend
fails closed rather than returning other resources or guessing how to filter
an unfamiliar response schema. MCP connection configuration is an account-wide
collection surface; tool-specific restrictions apply to `mcp:execute`, not to
connection management.

Workflow names are not credential identities. Deleting a workflow invalidates
its generation ID; recreating it under the same name does not revive old keys.
Workflow generation IDs are returned by `GET /api/tokens/catalog` and are
never chosen by the client. Deployment, deletion, resource lookup and webhook
issuance share the workflow's lifecycle lock, so stale inventory cannot mint a
credential for a deleted/recreated definition. Resource discovery is bounded and
can return a warning during an authoring outage without blocking key revocation.

## Creating and using a key

Use **Settings → API keys**. Give every integration a descriptive name, choose
only its required permissions/resources, and choose an expiry interval or an
exact date/time. The secret is displayed once in a copy dialog. It is not
persisted in browser storage and cannot be retrieved later.

For regular APIs use:

```http
Authorization: Bearer <the-one-time-secret>
```

An invalid explicitly supplied bearer credential never falls back to a valid
cookie. Normal APIs do not accept keys in URL parameters. Browser cookie writes
still need CSRF; API keys are not cookie credentials.

Default expiry is 90 days; maximum lifetime is one year. All user-created keys
expire. Rotation atomically revokes the old key and returns a replacement with
the same permissions and original expiry. Expired keys must be replaced with a
new key, and permissions are revalidated during rotation.

### Workflow webhook links

**Copy webhook URL** first creates a new key, then copies the returned URL.
It does not create keys when rendering, previewing or loading a workflow.
Default lifetime is seven days; the API supports 1–30 days. The link is reusable
until expiry/revocation and can only trigger that exact workflow. It cannot read
code, inspect runs, deploy, schedule or use the ordinary manual-run endpoint.

The webhook accepts an empty body or valid JSON, with a 1 MiB payload ceiling
and a 60-request/minute per-key admission budget. Duplicate deliveries are
ordinary triggers; callers needing exactly-once business effects should include
an event ID and implement deduplication in their workflow.

The copy dialog shows the expiry and links to the key's usage/revocation page.
Rotation of a webhook key also returns a replacement URL when its workflow and
public instance URL remain available.

Webhook URLs necessarily contain a bearer secret. Backend Uvicorn access logs
strip query strings, responses use `no-store`/`no-referrer`, and the existing
OpenTelemetry export allowlist excludes URLs, headers and payloads. **Operators
must also suppress/redact query strings in any independently configured reverse
proxy, analytics or upstream integration logs.** Header-based webhook
credentials are supported when the receiving integration can set headers.
Never paste a webhook URL into a public issue or shared analytics event.

## MCP

The backend `/mcp/` transport accepts its internal service credential or an
ordinary API key with MCP discovery/execution permissions. User cookies and
workflow webhook keys are not MCP credentials.

- Listing tools requires `mcp:read` and only exposes account-facing tools for
  which the key also has the corresponding API scope.
- Calling a tool requires `mcp:execute`, any tool restriction, **and** the
  underlying API scope/resource/ownership checks.
- External MCP requests are forwarded to HTTP using the same API key, never
  exchanged for `INTERNAL_TOKEN` or an administrator session.
- External callers cannot supply delegated internal actor envelopes.

MCP HTTP requests and their in-process backend tool dispatches are both retained
as audit records. Tool names distinguish dispatches from transport requests;
usage counts include both admissions. RPC failures are marked as failures even
when the MCP HTTP response status is 200.

## Usage history and retention

Settings shows key state, prefix, expiry, last activity, seven-day counts and
failures, a 30-day daily chart, grants and paginated request history. Every
attributable request is recorded, including forbidden, expired and revoked-key
attempts. Unknown credentials produce a general authentication-failure event
without storing their secret.

History stores method, route template, resolved resource ID, required scope,
status, duration, outcome, request ID, MCP tool and workflow run ID. There are no
raw URL query strings, authorization headers, request/response bodies or secrets
in this history. No source IPs are retained in per-key history.

Detailed records are retained for 90 days and daily aggregates for one year by
hourly maintenance. Retention is currently fixed by backend constants and shown
explicitly in Settings. Revoking a key preserves its retained audit history.
Admissions are persisted before execution; a failure to persist them prevents
the operation. A process crash can leave a `started` record rather than falsely
claiming the request succeeded.

## Adding an API

1. Add the route and its request/response validation.
2. Add its exact **method + route template** to `data/api-policies.json`.
3. Choose the scope, accepted authentication class and resource resolver. Add a
   new scope to `api_policy.DOMAINS` only when its authority materially differs.
4. Keep ownership checks; a scope is not a resource-sharing grant.
5. For mounted APIs classify the mount and every child route. For a new
   WebSocket choose authenticated admission or a reviewed single-use ticket
   verifier; arbitrary anonymous sockets are denied.
6. Add anonymous, wrong-scope, wrong-resource, cross-account, expiry/revocation
   and audit tests. Do not add a path-prefix or administrator bypass.

Run `uv run pytest tests/backend/test_api_keys.py`, the existing account/privacy
suites, `uv run ruff check .`, and the frontend unit/browser tests. The key
security suite is part of the main CI workflow.
