# Account authentication clients

The web client always talks to its same origin. A previously saved
`nautionette.server` does not redirect web credentials. Legacy `nautionette.token`
is removed on startup and is never imported as an account credential.

## Session transport

- Web: HttpOnly server session cookie, `credentials: same-origin`, and
  `X-CSRF-Token` from `/api/auth/me` on writes. No account bearer token is stored.
- Native: an HTTPS server origin, memory-only access token and an OS-protected
  rotating refresh token. Native requests omit cookies and use Authorization.
  Refresh is single-flight; requests retry at most once after refresh and once
  after an explicit recent-auth challenge.
- SSE: browser EventSource with same-origin cookies; native fetch streaming with
  an Authorization header. Credentials are never appended to stream URLs.
- The backend remains authoritative for ownership, permissions, session expiry,
  email delivery, rate limits and recent authentication. Hiding navigation is
  not authorization.

Public links use fragments `#bootstrap=…`, `#invite=…`, `#verify=…`, and `#reset=…`.
The client consumes and strips these before router redirects/history seeding or
API exchanges. Invitations can also be entered as a code or pasted fragment link.
Invite lookup is `POST /api/auth/invitation` with `{code}`, never a secret-bearing
query string. Email-less invitations have an explicit **Copy code** action. Administrators
can also issue one-time password reset codes; **Forgot password? → Use a reset code**
works without email. Optional recovery-email fields may be left blank or removed.
Passwords require at least 15 characters. Changing/resetting a password signs devices out.
There are no external identity-provider buttons in this release.

Bootstrap and invitation registration use the same public payloads on every
platform. Native clients sign in separately after successful registration if the
response has no native tokens. New registrations are active immediately after a valid
single-use invite code; email and SMTP are never a registration or sign-in requirement.
The optional recovery address can be verified later. Refresh accepts either a complete
session response or tokens followed by `/me`, without recursively refreshing `/me`.

## Native vault boundary

- Electron uses safeStorage (Keychain/DPAPI/Linux secret service), an atomic
  encrypted file with mode 0600, and a top-level bundled-renderer IPC check.
  Linux `basic_text` or unavailable encryption is rejected; no plaintext fallback.
- iOS uses device-only, when-unlocked Keychain items. A main-frame-only
  document-start capability protects the vault because WK script message handlers
  can otherwise be invoked by generated subframes.
- Android uses a non-exportable Keystore AES key and AES-GCM ciphertext with the
  server origin as authenticated additional data. Backup is disabled to avoid
  restoring ciphertext without its device key. The vault requires an
  origin-scoped, main-frame-only document-start capability, including when
  Capacitor falls back to its legacy JavaScript interface. Old Android WebViews
  without DOCUMENT_START_SCRIPT cannot access the vault and must be updated.
- Both Capacitor bridges expose only get/set/remove for refresh credentials;
  neither exposes generic filesystem, HTTP or key export operations. Capabilities
  are ephemeral, never sent to the server or persisted.

## Private client state

History, composer drafts, remembered chat settings, agent selection and outbox
keys include both the server origin and account ID. Old unscoped records are not
read or replayed. Signing out closes streams, stops delivery, clears all shared
reactive lists and unmounts the private shell. An account epoch fences late HTTP
responses; response account headers and event handshakes also fence cookie identity
changes in another tab. Delivery checks its original account scope before sending.
Native vault mutations are serialized so an older asynchronous write cannot replace
credentials for a newer login.
Cross-tab logout broadcasts only a timestamp, not credentials. Previously queued
messages remain available only when that same account signs back in.

## Verification

```sh
npm ci
npm test
npx playwright test tests/account-auth.spec.js tests/settings-navigation.spec.js tests/settings-redesign.spec.js --workers=1
npm run build
```

Existing feature tests import `tests/account-fixture.js` to explicitly provide an
authenticated API session; the application contains no test bypass. Native vault
unit tests substitute the OS encryption primitive while executing the real
persistence implementation. Native bridge wiring/security policy is checked in
source tests. Actual Keychain/Keystore integration, device unlock/relock, app
relaunch, safe areas and keyboard behavior still require platform builds and real
hardware. Do not equate browser emulation or source checks with native device tests.
