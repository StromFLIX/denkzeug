# Authentication and operator recovery

Denkzeug authenticates people with individual accounts, not a shared deployment
password. `APP_TOKEN` is obsolete: it grants **no** human or service access. Missing
credentials never enable an open mode. Browser sessions use secure, HttpOnly
cookies; native/device clients must use their supported sign-in flow. The private
`INTERNAL_TOKEN` still authenticates stack services; it is not a user login.

## Configure the external origin before upgrading

Set `DENKZEUG_PUBLIC_URL=https://your-app.example` (or the compatibility fallback
`APP_URL`) to the app's canonical external origin, not the promotional website.
Keep both variables equal when both are supplied. Terminate TLS at a trusted
reverse proxy, route the app to the backend and preserve HTTPS/WebSocket support.
Do not publish broker, gateway, database, storage or Temporal ports. Do not trust
arbitrary forwarded headers from the public internet.

Secure sessions are the default, including behind a TLS-terminating proxy. For an
isolated **development** stack on localhost only, explicitly set all of:

```dotenv
APP_ENVIRONMENT=development
DENKZEUG_PUBLIC_URL=http://localhost:8080
APP_URL=http://localhost:8080
DENKZEUG_AUTH_ALLOW_INSECURE_LOCALHOST=true
```

This is not a switch for staging, production, a LAN IP, or remote HTTP. Do not
turn authentication off to make a smoke test pass. Plain SMTP is likewise only a
development option; do not use it to work around a mail certificate error.

## Claim a new or upgraded instance

Back up first. On an existing installation, retain the original namespace,
volumes, SQLite database, encryption/master keys and service credentials. Creating
a new volume or changing `STACK_NAMESPACE` is not migration.

**The operator/current owner must claim the first administrator account.** A
release/deployment job must not silently appoint an administrator, choose a
permanent password, or print setup credentials to deployment logs. Authentication
schema migration and the first-admin claim preserve legacy content; the initial
administrator receives the legacy ownership atomically, not a public fallback
identity. Once claimed, bootstrap must not reset accounts or transfer ownership.

On the deployment host, issue a short-lived, single-use setup token explicitly:

```sh
docker compose exec backend python -m nautionette_backend.auth_cli setup-token
```

Enter that token in the app's initial setup flow and create your administrator
account. Treat the token like a password: never paste it into a chat, CI log, issue,
or URL query string. Close/clear the terminal when finished. An already claimed
instance cannot be claimed again by reissuing a setup token.

For **new unattended installations only**, provision the bootstrap values through
the deployment secret store:

```dotenv
DENKZEUG_BOOTSTRAP_ADMIN_USERNAME=owner
DENKZEUG_BOOTSTRAP_ADMIN_EMAIL=owner@example.com
DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD_FILE=/run/secrets/denkzeug-bootstrap
```

Mount that secret read-only into the backend. `_FILE` names are container paths,
not host paths: passing a path through `.env` does not mount it. For example,
create a private `compose.secrets.yaml` (never commit passwords or secret files):

```yaml
services:
  backend:
    volumes:
      - /etc/denkzeug/bootstrap-password:/run/secrets/denkzeug-bootstrap:ro
      - /etc/denkzeug/smtp-password:/run/secrets/denkzeug-smtp:ro
```

Use `docker compose -f docker-compose.yaml -f compose.secrets.yaml ...` consistently.
Restrict host file permissions to the operator/backend identity. Docker secrets
are another mounting option. Plain `DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD` and
`DENKZEUG_SMTP_PASSWORD` exist for deployment systems without mounted secrets, but
container environment inspection can reveal them. Do not specify conflicting
file and inline secrets. Remove the bootstrap secret/values after a successful
claim and recreate the backend. No permanent password is generated in server logs.

## Password reset mail

Configure SMTP in the **backend only**, not in the agent containers:

```dotenv
DENKZEUG_SMTP_HOST=smtp.example.com
DENKZEUG_SMTP_PORT=587
DENKZEUG_SMTP_USERNAME=denkzeug-mail
DENKZEUG_SMTP_PASSWORD_FILE=/run/secrets/denkzeug-smtp
DENKZEUG_SMTP_FROM=Denkzeug <no-reply@example.com>
DENKZEUG_SMTP_TLS_MODE=starttls
```

`starttls` upgrades an SMTP connection and validates the server certificate.
For implicit TLS use `ssl` and the provider's corresponding port (usually 465).
`plain` is development-only. Configure the sender's SPF/DKIM/DMARC and permit the
host's outbound SMTP traffic. Test reset delivery with a controlled mailbox after
an origin or SMTP change; links must point to this environment's HTTPS origin.

Reset requests must not disclose whether an account exists. Reset tokens are
short-lived and single-use; do not forward reset email or copy its contents into
logs. Missing/broken SMTP does not weaken login or expose a permanent fallback
password. Diagnose connectivity, TLS, sender authorization and canonical origin;
never disable certificate checking to repair delivery.

## Local operator recovery (including unavailable SMTP)

Host/container access is administrative access. On the correct deployment's
backend, use an interactive terminal:

```sh
docker compose exec backend python -m nautionette_backend.auth_cli recover-password USERNAME
```

The CLI prompts twice without echo and revokes existing sessions on successful
recovery. Password policy is enforced by the same auth store as normal password
changes. For a noninteractive operator session, mount a restricted temporary file
and use `--password-file /run/secrets/recovery-password`; do not pass a password as
a command argument or embed it in shell history. Remove the file after recovery.
The CLI intentionally does not print passwords or raw exceptions. If it fails,
verify the target container, `DATA_DIR`, file permissions and username locally.
Do not directly edit password hashes or authentication tables in SQLite.

## Users, roles and grants

Use individual accounts; never share the administrator's session or device
credential. Administrators manage users and instance-wide security/configuration.
Resource ownership and explicit grants determine access to chats and other
resources; a role name alone is not consent to share somebody else's private chat.
Grant only the needed projects, agents and tools. Revoke lost devices and obsolete
sessions, and disable departed accounts. Test both authorized and denied access
after changing grants.

Chats are private to their owner unless explicitly shared by a supported grant.
This HTTP/API boundary is **not** a hardened mutually-untrusted tenant boundary:
Denkzeug runs trusted automation with shared deployment resources. Operators can
read backups and the database. Agents with filesystem/project, shell, workflow or
external integration access may reach information or accounts outside a chat's
API grants. Workflows, tools, projects and integration credentials must therefore
be given only to trusted collaborators. See [self-hosting security](self-hosting.md#security-and-data).

Provider-identity records prepare the account model for OIDC identity linking.
They do **not** mean live OIDC/SSO is configured or supported. GitHub/Copilot model
or project integration OAuth is also not a Denkzeug user sign-in provider.

## Native clients and device builds

Browser, desktop, Android and iOS clients connect to the **app** origin, never the
website origin. Upgrade/rebuild clients with the server authentication change;
old builds asking only for `APP_TOKEN` cannot log in. Sign in with an individual
account and authorize devices through the supported flow. Never bake passwords,
setup tokens, SMTP credentials or `INTERNAL_TOKEN` into a binary or build artifact.
Device credentials must be stored in platform-protected storage and revocable;
a copied/stolen bearer credential is secret access until revoked/expired. Test
sign-in, expiry, logout and lost-device revocation on every distributed platform.

## Backups, staging and maintenance

The backend SQLite database includes account identities, password hashes,
session/token digests, ownership and grants. Back it up consistently with
`maxio-data`, native chat sessions, projects and the other datasets listed in
[self-hosting](self-hosting.md#security-and-data). Protect backups as secrets,
preserve permissions and master keys, and test a restore in an isolated stack.
Never copy only a live SQLite main file while WAL writes are active.

A production-data staging clone also clones identity/security state. Use an
independent `INTERNAL_TOKEN`, HTTPS origin, secrets and SMTP test sink. Do not
send production password-reset mail from staging. Existing copied credentials
and sessions are sensitive even when cookies are origin-scoped; explicitly
revoke copied sessions/device grants using the supported administration tools
before granting staging access. Never regenerate bootstrap configuration to
seize an already claimed database. Follow the [staging quarantine](staging.md)
for copied schedules, agents and remote integration side effects as well.

Deployment health checks must distinguish anonymous liveness (`/healthz`) from
privileged component readiness and functional tests. Keep service-protocol
credentials separate from a revocable least-privilege test user's device
credential. Do not restore `APP_TOKEN` as a compatibility bypass. After an
upgrade, verify anonymous private API denial, initial claim/login, a normal
user's private chat, cross-user denial, SMTP reset, session revocation and each
supported native client before declaring the migration complete.
