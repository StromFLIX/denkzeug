# Accounts, invitations and private workspace access

Denkzeug uses individual username/password accounts. **Email is optional everywhere**: bootstrap, invitations, registration and password recovery all work without SMTP or an email address. There is no public unrestricted registration or anonymous API mode.

## First administrator and existing data

The person operating the existing instance claims the first administrator account. In the same SQLite transaction, Denkzeug assigns **all existing unowned chats, messages/attachments/outputs through their chat, runs, workflows, apps and workspace resources** to that account. Existing legacy artifact files are indexed for that owner. Later invited accounts never inherit existing conversations. Do not initialize a real instance using a disposable QA identity.

Back up the database, private object storage and persistent volumes before upgrading. Keep the existing Compose project, namespaces, bucket names and volumes; never replace the deployment's `.env` wholesale with `.env.example`.

Authentication is always enabled. The old `APP_TOKEN` does not authenticate users; it is neither an account password nor an API-key compatibility bypass. `INTERNAL_TOKEN` remains a separate required service credential and must not equal the obsolete `APP_TOKEN`. Upgrade existing native clients to the account-capable release. Existing external scripts using the old token require a deliberately authenticated replacement.

Set the canonical application origin:

```dotenv
DENKZEUG_PUBLIC_URL=https://app.example.com
```

`APP_URL` remains a fallback. Use this environment's HTTPS origin, not a marketing-site URL, another environment, a path or a request-supplied Host header. Links use fragment secrets, not query strings.

### Interactive setup

When no administrator exists, normal user APIs remain locked. With a configured public origin, startup writes an expiring, single-use setup link into **restricted backend logs** (or a pasteable setup code when no origin is configured). Open it and choose a username/password; leave recovery email blank if you do not want it. The link expires after 30 minutes and changes after restart/reissuance. Never put it in tickets, chat messages, public build output or telemetry.

An operator with container access can explicitly issue a replacement setup code:

```sh
docker compose exec backend python -m nautionette_backend.auth_cli setup-token
```

Paste the printed code through **Enter setup code** on the first-run screen. This command refuses once an administrator exists. It is not a permanent backdoor or an account reset mechanism.

### Environment provisioning

For unattended initialization, supply a username and password file. **Email is not required.**

```dotenv
DENKZEUG_BOOTSTRAP_ADMIN_USERNAME=owner
DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD_FILE=/run/secrets/denkzeug-admin
# Optional:
DENKZEUG_BOOTSTRAP_ADMIN_EMAIL=
```

Mount that secret read-only using a private Compose override or Docker secrets; `_FILE` paths refer to the backend container, not the host. Restrict its permissions. `DENKZEUG_BOOTSTRAP_ADMIN_PASSWORD` is an alternative for systems without secret-file support, but environment inspection exposes it to the operator. Do not set both password sources.

Provisioning happens only once and requires replacing the temporary password after sign-in. Changing environment values does not overwrite existing accounts or reset passwords. Remove temporary bootstrap credentials after successful initialization. Permanent passwords are never printed in application logs.

Explicit localhost development is supported with `APP_ENVIRONMENT=development`, `DENKZEUG_AUTH_ALLOW_INSECURE_LOCALHOST=true` and a matching localhost `DENKZEUG_PUBLIC_URL`. These are not switches for production, staging, remote HTTP or disabling authentication. Native clients require HTTPS origins.

## Copy/paste invitations—no email needed

In **Settings → Account & workspace → Access policies**, enable **Invitation only**. In **Invitations**:

1. Optionally add a private note, such as “For Sam”.
2. Leave email blank for a code-only invitation. Member is the default role.
3. Create the invitation and select **Copy code**. Send it through whatever secure channel you prefer.
4. The recipient selects **Use an invitation**, pastes the code, and chooses a username/password. They may leave recovery email blank and start using the account immediately.

A copyable link is also provided when a canonical public URL is configured. A link is a convenience, never a requirement for using the code. Codes are high-entropy secrets, single-use, hashed in storage, expiring and revocable. The default expiry is seven days. They are shown only on creation; create a replacement if the code is lost. Reissuing an email-addressed invitation supersedes previous outstanding invitations for that delivery address. Independent code-only invitations do not revoke each other.

An invitation's email is optional **delivery metadata**, not an account identifier or mandatory mailbox check. The recipient chooses whether to add any recovery email. SMTP being absent does not block invitation creation, acceptance or sign-in.

**Paused** blocks both creation of invitations and redemption of outstanding codes, without preventing existing accounts signing in. Resuming accepts only still-valid, unrevoked codes. There is no unrestricted sign-up mode. Administrative invitations/role elevation require deliberate confirmation and recent authentication.

## Password recovery without email

An administrator can open **Users → Create reset code** for an active password account. It requires recent authentication and is audited. Copy the code or optional link and share it with that person securely.

At sign-in, choose **Forgot password? → Use a reset code**. Paste the code, choose a new password and sign in again. No email, SMTP or previous password is required. Reset codes expire in 30 minutes, can be used once, and are replaced when reissued. Successful redemption revokes the target account's existing sessions. Issuing a code alone does not sign the person out.

Administrators cannot retrieve passwords. Creating a recovery code nevertheless gives its recipient the ability to replace a password, so this is a powerful, audited administrative capability. Recent account security events are available through the authenticated account API. A verified optional recovery address can receive a security notice, but delivery is not required for recovery.

If the only administrator loses access, the local operator can recover it using a non-echoed terminal prompt or mounted password file:

```sh
docker compose exec -it backend python -m nautionette_backend.auth_cli recover-password owner
# Non-interactive alternative:
docker compose exec backend python -m nautionette_backend.auth_cli recover-password owner --password-file /run/secrets/recovery-password
```

The command requires server/container access, revokes sessions/refresh families, and requires replacing the temporary password at the next sign-in. Never place passwords directly in command arguments, workflow inputs, chat messages or shell history.

## Optional email convenience

A person may add, change or remove **Recovery email (optional)** under Profile & security, confirming their password. Email recovery is enabled only after verifying the chosen address. An unverified or absent email never prevents username/password sign-in. Removing an address is supported even when SMTP is unavailable.

Optional SMTP settings belong to the backend only:

```dotenv
DENKZEUG_SMTP_HOST=smtp.example.com
DENKZEUG_SMTP_PORT=587
DENKZEUG_SMTP_USERNAME=denkzeug-mail
DENKZEUG_SMTP_PASSWORD_FILE=/run/secrets/denkzeug-smtp
DENKZEUG_SMTP_FROM=no-reply@example.com
DENKZEUG_SMTP_TLS_MODE=starttls
```

Use a bare sender mailbox. `starttls` and `ssl` verify the server certificate; `plain` is development-only. Configure SPF/DKIM/DMARC and outbound SMTP connectivity. Never disable certificate validation to work around an error. Empty/misconfigured optional SMTP does not weaken authentication or make code-based onboarding/recovery unavailable.

Email reset requests give a non-enumerating response. Mail configuration/key-storage errors are checked before account lookup. Verification/reset links use `#verify=`/`#reset=` fragments and POST redemption, not proxy-visible query parameters. Invite lookup also uses a POST body. Do not enable authentication request-body logging. Account responses are non-cacheable.

Transactional mail is queued durably with encrypted payloads, bounded retry and secret-free delivery errors. The mail encryption key is stored under `DATA_DIR/account-mail.key`, mode 0600; full backups containing the key and database are sensitive. Admins can inspect sanitized mail delivery status through the account API and queue a test to their optional email. A missing/unverified address is not an error for normal account use.

## Private data and explicit sharing

- Chats, branches, drafts, attachments, outputs and private run results belong to their account. An administrator role does not bypass private HTTP ownership checks, searches, diagnostics or download authorization.
- Existing anonymous data is assigned once to the initial administrator, never shared with each invited person.
- Saved agents, projects/repositories, workflows, miniapps and individual tools can be shared explicitly through **Access policies → Shared resources**. Sharing an agent is not sharing any conversation with it. Share its needed projects/tools separately.
- Member agents use the reviewed default container in **conversation/shared-tool mode**. Local shell/filesystem tools, native creative processes, extensions and arbitrary infrastructure administration remain administrator-only. The runner/broker derives this policy from the real account owner, not browser-supplied flags, including agent activities started by a member-owned workflow. Required grants/status are rechecked before execution and during chat control. Fully trusted coding operators can be given administrator access explicitly; custom containers must not silently bypass member policy.
- Shared workflow results use account-specific delivery chats; one member's run must not change another person's private delivery target.
- Administrators manage users, invitations, registration policy and shared infrastructure. The last active administrator cannot be suspended or demoted.
- Suspension revokes sessions immediately, fences delegated calls, stops owned chats and queues cancellation/pause of owned automation. Infrastructure outages are retried; the account API explicitly reports when automation pause is still pending. Reactivation does not silently re-enable schedules.

This is a **trusted self-hosted workspace**, not hostile multi-tenant isolation or end-to-end encryption. The server operator controls files, backups, logs, powerful code and integration credentials. Granting access to a saved executable resource is a trust decision; HTTP ownership does not isolate malicious code from shared hosts, networks or repositories. Do not share generic control-plane HTTP/code-execution tools or privileged custom runtimes with non-operators: a configured tool can act with its external identity. Private application data checks do not claim to protect against the host operator or a deliberately shared workflow that publishes its own results.

## Sessions, devices and service authority

Browser sessions are opaque, server-side, HttpOnly/Secure/SameSite cookies; writes require CSRF proof and origin checks. Native apps keep short-lived access credentials in memory and rotating refresh credentials in OS-protected storage. No account credential is persisted in localStorage. Missing/unavailable OS encryption fails closed rather than falling back to plaintext. Native origins are exactly allowlisted, not wildcard credentialed CORS.

Account-scoped caches/drafts/outboxes and response epochs prevent one account inheriting another's pending writes/content. Response account headers and event handshakes fence browser account changes. Sign-out/revocation closes long-lived transports. Streams never carry account tokens in URLs.

Agentgateway's internal transport credential is **not** a human administrator. Backend operations need a live turn capability resolving the owning account and its current status/role. Capabilities are short-lived process-memory authority, not persisted master tokens or model-visible schemas. Account management routes are deliberately excluded from agent MCP. Workflow-MCP is also capability-scoped: its network tool calls go through the same private API checks, and its REST authoring surface requires service authentication. First-party SDK activity calls use verified Temporal provenance and short-lived per-tool authority; neither private run history nor workflow deployment is available through an unscoped MCP backdoor. The rebuilt agent image is required for this delegated transport.

`/internal/system` and `/internal/settings/storage` expose read-only operational readiness to `X-Internal-Token`; they do not grant human private-data access. Never expose that credential to browsers, native apps, model prompts or test artifacts. Configure trusted proxy forwarding explicitly; do not blindly trust internet-supplied forwarding headers for authentication/rate limits.

## SSO readiness

Local passwords are optional credentials in the account model; external identities have independent provider/issuer/subject keys. The provider interface and explicit link/unlink rules are ready for a maintained OIDC authorization-code/PKCE adapter. Matching email claims never silently link accounts or bypass invitations. Removing the last usable sign-in method is rejected.

This release does **not** enable a live SSO provider or show a nonfunctional SSO button. External identity-provider configuration and protocol integration follow separately.

## Staging and verification

A copied production database includes account/session state. Use an independent `INTERNAL_TOKEN`, public origin, secrets and optional SMTP sink. Never send real recovery mail from staging. Do not reset real users to create a QA identity.

Functional staging scripts use a dedicated QA account via `DENKZEUG_SMOKE_USERNAME` and `DENKZEUG_SMOKE_PASSWORD[_FILE]`. Sessions are acquired normally for each process, refreshed in memory, and signed out best-effort. No static ten-minute token is a durable CI credential. A short-lived manual override requires explicit `DENKZEUG_ALLOW_DEVICE_TOKEN=true`. Provision any QA account deliberately after the real initial administrator claims ownership.

Before declaring rollout verified, test initial ownership, email-free code registration/recovery, cross-account denial, session/device revocation, delegated permissions, migration/backup restore and each supported native platform. Optional email delivery needs its own controlled-mailbox verification only if configured. Browser emulation and mocked OS stores are not physical iOS/Android/desktop verification.
