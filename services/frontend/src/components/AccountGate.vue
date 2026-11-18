<template>
  <main class="account-entry">
    <section class="account-entry__intro">
      <div class="account-entry__brand"><BrandMark /><span>{{ brand.name }}</span></div>
      <h1>Your space<br>to think and build.</h1>
      <p>Conversations, tools and workflows.<br>One workspace, your account.</p>
    </section>
    <section class="account-entry__panel" aria-labelledby="account-title">
      <div v-if="!account.ready || loading" role="status">Opening your workspace…</div>
      <template v-else-if="!connected">
        <h2 id="account-title">Connect to your workspace</h2>
        <p class="muted">Enter the HTTPS address of your Denkzeug server.</p>
        <p v-if="error" class="account-error" role="alert">{{ error }}</p>
        <form class="account-form" @submit.prevent="connect">
          <label>Server address<input v-model="serverUrl" class="field" type="url" inputmode="url" required autocapitalize="none" spellcheck="false" autocomplete="url" placeholder="https://denkzeug.example.com" :disabled="busy" /></label>
          <button class="btn btn--primary" :disabled="busy">{{ busy ? 'Connecting…' : 'Connect' }}</button>
        </form>
      </template>
      <template v-else>
        <h2 id="account-title">{{ titles[mode] }}</h2>
        <p v-if="mode === 'login'" class="muted">Sign in with your workspace username.</p>
        <p v-if="mode === 'bootstrap'" class="muted">Create the first administrator. Your setup token is only used once.</p>
        <p v-if="mode === 'setup'" class="muted">Open the setup link from your server logs to create the first administrator.</p>
        <p v-if="mode === 'invite' && invitation" class="muted">Join this workspace as {{ invitation.role }}. This code expires {{ date(invitation.expires_at) }}. Email is optional.</p>
        <p v-if="mode === 'forgot'" class="muted">Ask an administrator for a one-time reset code—no email needed. If you added and verified a recovery email, you can also request a link below.</p>
        <p v-if="mode === 'change'" class="muted">Choose a new password before opening your workspace.</p>
        <p v-if="error" class="account-error" role="alert">{{ error }}</p>
        <p v-if="notice" class="account-notice" role="status">{{ notice }}</p>
        <form v-if="!['setup', 'verify'].includes(mode) && (mode !== 'forgot' || meta.mail_available)" class="account-form" @submit.prevent="submit">
          <label v-if="mode === 'invite'">Invitation code or link<input v-model="code" class="field" required autocapitalize="none" autocomplete="off" spellcheck="false" :disabled="busy" @change="checkInvitation" /></label>
          <label v-if="mode === 'bootstrap' && !entryToken">Setup code<input v-model="setupCode" class="field" required autocomplete="off" autocapitalize="none" spellcheck="false" :disabled="busy" /></label>
          <label v-if="mode === 'reset' && !entryToken">Reset code or link<input v-model="resetCode" class="field" required autocomplete="off" autocapitalize="none" spellcheck="false" :disabled="busy" /></label>
          <label v-if="['login', 'bootstrap', 'invite'].includes(mode)">Username<input v-model="username" class="field" required autocomplete="username" autocapitalize="none" spellcheck="false" :disabled="busy" /></label>
          <label v-if="['forgot', 'bootstrap', 'invite'].includes(mode)">{{ mode === 'forgot' ? 'Recovery email' : 'Recovery email (optional)' }}<input v-model="email" class="field" :required="mode === 'forgot'" type="email" autocomplete="email" :disabled="busy" /></label>
          <label v-if="mode === 'change'">Current password<input v-model="currentPassword" class="field" required type="password" autocomplete="current-password" :disabled="busy" /></label>
          <div v-if="['login', 'bootstrap', 'invite', 'reset', 'change'].includes(mode)" class="account-password-control"><label for="account-password">{{ mode === 'login' ? 'Password' : 'New password' }}</label><div class="account-password-input"><input id="account-password" v-model="password" class="field" :type="passwordVisible ? 'text' : 'password'" required :minlength="mode === 'login' ? undefined : 15" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" :disabled="busy" /><button type="button" class="btn" :aria-label="passwordVisible ? 'Hide password' : 'Show password'" :aria-pressed="passwordVisible" :disabled="busy" @click="passwordVisible = !passwordVisible">{{ passwordVisible ? 'Hide' : 'Show' }}</button></div></div>
          <template v-if="['bootstrap', 'invite', 'reset', 'change'].includes(mode)">
            <p class="caption muted">Use at least 15 characters. A unique passphrase works well.</p>
            <label>Confirm password<input v-model="confirmation" class="field" type="password" required autocomplete="new-password" :disabled="busy" /></label>
          </template>
          <button class="btn btn--primary" :disabled="busy">{{ busy ? 'Please wait…' : buttons[mode] }}</button>
        </form>
        <div class="account-entry__links">
          <template v-if="mode === 'login'">
            <button class="btn" @click="changeMode('forgot')">Forgot password?</button>
            <button v-if="meta.registration_mode === 'invite_only'" class="btn" @click="changeMode('invite')">Use an invitation</button>
          </template>
          <button v-if="mode === 'forgot'" class="btn btn--primary" @click="changeMode('reset')">Use a reset code</button>
          <button v-if="mode === 'setup'" class="btn" @click="changeMode('bootstrap')">Enter setup code</button>
          <button v-if="!['login', 'change', 'setup'].includes(mode)" class="btn" @click="changeMode('login')">Back to sign in</button>
          <button v-if="(mode === 'setup' || error) && !busy" class="btn" @click="loadMeta">Try connection again</button>
          <button v-if="account.user" class="btn" @click="logout">Sign out</button>
        </div>
        <div v-if="isNative && !account.user" class="account-entry__server">
          <span class="caption muted">{{ serverUrl }}</span>
          <button type="button" class="btn btn--sm" :disabled="busy" @click="changeServer">Change server</button>
        </div>
      </template>
    </section>
  </main>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { account, consumeAuthFragment, isNative } from '../auth-session.js'
import { accountApi } from '../account.js'
import { server } from '../api.js'
import BrandMark from './BrandMark.vue'
import { brand } from '../brand.js'
const props = defineProps({ fragment: { type: Object, default: null } })
const emit = defineEmits(['done'])
const titles = { login: 'Welcome back', bootstrap: 'Set up your workspace', setup: 'Your workspace needs an administrator', invite: 'Accept your invitation', forgot: 'Reset your password', reset: 'Choose a new password', verify: 'Verify your email', change: 'Change your password' }
const buttons = { login: 'Sign in', bootstrap: 'Create administrator', invite: 'Create account', forgot: 'Send reset link', reset: 'Reset password', change: 'Change password' }
const fragment = props.fragment || consumeAuthFragment()
const entryToken = ref(fragment?.token || ''), setupCode = ref(''), resetCode = ref('')
const mode = ref(account.user?.must_change_password ? 'change' : fragment?.flow === 'invite' ? 'invite' : fragment?.flow || 'login')
const passwordVisible = ref(false)
const connected = ref(!isNative || Boolean(account.user))
const meta = ref({}), loading = ref(false), busy = ref(false), error = ref(account.error), notice = ref(''), invitation = ref(null)
const serverUrl = ref(server.url), username = ref(''), email = ref(''), password = ref(''), confirmation = ref(''), currentPassword = ref(''), code = ref(fragment?.flow === 'invite' ? fragment.token : '')
const date = value => new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value).toLocaleString()
function changeMode (value) { passwordVisible.value = false; if (value !== fragment?.flow) entryToken.value = ''; mode.value = value; error.value = notice.value = password.value = confirmation.value = currentPassword.value = ''; invitation.value = null }
async function loadMeta () {
  error.value = ''
  if (isNative && !serverUrl.value) return false
  try {
    // Validate the candidate before saving it or sending any account payload.
    const data = isNative && !connected.value
      ? await accountApi.probeServer(serverUrl.value)
      : await accountApi.meta()
    if (isNative && data.native_min_version > 1) throw new Error('Update the app to sign in to this workspace.')
    meta.value = data
    if (data.setup_required && mode.value === 'login') mode.value = 'setup'
    else if (!data.setup_required && mode.value === 'setup') mode.value = 'login'
    if (isNative) {
      if (!connected.value) server.url = serverUrl.value
      serverUrl.value = server.url
    }
    return true
  } catch (e) {
    error.value = e.message
    return false
  }
}
function changeServer () {
  connected.value = false
  changeMode('login')
  username.value = email.value = code.value = setupCode.value = resetCode.value = entryToken.value = ''
}
async function connect () {
  if (busy.value) return
  busy.value = true
  try {
    if (await loadMeta()) { connected.value = true; await completeEntryLink() }
  } finally { busy.value = false }
}
function invitationCode () {
  const value = code.value.trim()
  if (!value.includes('://')) return value
  try {
    const link = new URL(value)
    const secret = new URLSearchParams(link.hash.slice(1)).get('invite')
    if (!secret) throw new Error()
    return secret
  } catch { throw new Error('Use an invitation code or a link containing an invitation fragment.') }
}
function recoveryCode () {
  const value = entryToken.value || resetCode.value.trim()
  if (!value.includes('://')) return value
  try { const hash = new URLSearchParams(new URL(value).hash.slice(1)); const token = hash.get('reset') || hash.get('reset-password'); if (!token) throw new Error(); return token } catch { throw new Error('Paste a reset code or reset link from your administrator.') }
}
async function checkInvitation () {
  if (!code.value.trim()) return
  try { code.value = invitationCode(); invitation.value = await accountApi.invitation(code.value); error.value = '' } catch (e) { invitation.value = null; error.value = e.message }
}
async function submit () {
  if (busy.value) return
  error.value = notice.value = ''
  if (mode.value !== 'login' && mode.value !== 'forgot' && password.value !== confirmation.value) { error.value = 'Passwords do not match.'; return }
  busy.value = true
  try {
    if (mode.value === 'login') await accountApi.login(username.value.trim(), password.value)
    else if (mode.value === 'bootstrap') await accountApi.bootstrap({ token: entryToken.value || setupCode.value.trim(), username: username.value.trim(), ...(email.value.trim() ? { email: email.value.trim() } : {}), password: password.value })
    else if (mode.value === 'invite') {
      const data = await accountApi.register({ code: invitationCode(), username: username.value.trim(), ...(email.value.trim() ? { email: email.value.trim() } : {}), password: password.value })
      if (data.verification_required) { changeMode('login'); notice.value = 'Check your email to verify your account, then sign in.' }
    } else if (mode.value === 'forgot') { await accountApi.forgot(email.value.trim()); notice.value = 'If an account matches that email, a reset link is on its way.' }
    else if (mode.value === 'reset') { await accountApi.reset({ token: recoveryCode(), password: password.value }); entryToken.value = resetCode.value = ''; changeMode('login'); notice.value = 'Password reset. Sign in with your new password.' }
    else if (mode.value === 'change') { await accountApi.changePassword({ current_password: currentPassword.value, password: password.value }); changeMode('login'); notice.value = 'Password changed. Sign in with your new password.' }
    password.value = confirmation.value = currentPassword.value = ''
    if (account.user && !account.user.must_change_password) emit('done')
    else if (account.user?.must_change_password) mode.value = 'change'
  } catch (e) { error.value = e.message } finally { busy.value = false }
}
async function logout () { try { await accountApi.logout(); changeMode('login') } catch (e) { error.value = e.message } }
async function completeEntryLink () {
  if (mode.value === 'invite' && code.value) await checkInvitation()
  if (mode.value === 'verify') {
    try { await accountApi.verify(entryToken.value); entryToken.value = ''; changeMode('login'); notice.value = 'Optional recovery email verified.'; if (account.user) { await accountApi.me(); emit('done') } } catch (e) { error.value = e.message }
  }
}
onMounted(async () => {
  loading.value = true
  if (!account.ready) await new Promise(resolve => { const stop = watch(() => account.ready, ready => { if (ready) { stop(); resolve() } }) })
  const available = await loadMeta()
  if (available) connected.value = true
  if (!error.value && account.error) error.value = account.error
  if (available) await completeEntryLink()
  loading.value = false
})
</script>

<style>
.account-entry { display: grid; grid-template-columns: minmax(0, 30rem) minmax(20rem, 32rem); justify-content: center; align-items: center; gap: clamp(2rem, 6vw, 5rem); flex: 1; min-height: 0; height: 100%; overflow-y: auto; padding: max(2rem, env(safe-area-inset-top)) max(2rem, env(safe-area-inset-right)) max(2rem, env(safe-area-inset-bottom)) max(2rem, env(safe-area-inset-left)); background: var(--surface-app); }
.account-entry__brand { display: flex; align-items: center; gap: .75rem; font-size: 1.4rem; font-weight: 600; letter-spacing: -.05em; }
.account-entry__brand .brand-mark { width: 2.5rem; height: 2.5rem; }
.account-entry__intro h1 { margin: 3rem 0 1.5rem; font-size: clamp(2rem, 4vw, 3.5rem); font-weight: 500; line-height: 1.15; letter-spacing: -.06em; }
.account-entry__intro p { color: var(--text-muted); line-height: 1.7; }
.account-entry__panel { padding: clamp(1.25rem, 3vw, 2.5rem); background: var(--surface-panel); border: 1px solid var(--border); border-radius: var(--radius-lg); max-width: 100%; }
.account-entry__panel h2 { font-size: 1.3rem; font-weight: 600; line-height: 1.4; margin: 0 0 .75rem; }
.account-form { display: grid; gap: 1rem; margin-top: 1.5rem; }
.account-form label { display: grid; gap: .45rem; font-size: .875rem; }
.account-form input, .account-form select { width: 100%; min-width: 0; min-height: 2.75rem; font-size: max(16px, .875rem); }
.account-form .caption { margin: -.5rem 0 0; }
.account-password-control { display: grid; gap: .45rem; font-size: .875rem; }
.account-password-input { position: relative; }
.account-password-input input { padding-right: 4.5rem; }
.account-password-input button { position: absolute; inset: 1px 1px 1px auto; width: 4rem; border: 0; background: transparent; font-size: .75rem; min-height: 42px; }
.account-entry__links { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: 1.25rem; }
.account-entry button { min-height: 44px; touch-action: manipulation; }
.account-entry button:active { background: var(--surface-hover); }
.account-entry :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.account-error { color: var(--danger, #b42332); border-left: 3px solid currentColor; padding-left: .75rem; overflow-wrap: anywhere; }
.account-notice { color: var(--text); background: var(--surface-raised); padding: .75rem; border-radius: var(--radius-sm); }
.account-entry__server { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 4px 12px; margin-top: 12px; padding-top: 8px; border-top: 1px solid var(--border); overflow-wrap: anywhere; }
@media (max-width: 850px) {
  .account-entry { grid-template-columns: minmax(0, 1fr); align-content: start; gap: 16px; padding: max(16px, env(safe-area-inset-top)) max(12px, env(safe-area-inset-right)) max(16px, env(safe-area-inset-bottom)) max(12px, env(safe-area-inset-left)); }
  .account-entry__brand .brand-mark { width: 36px; height: 36px; }
  .account-entry__intro h1, .account-entry__intro p { display: none; }
  .account-entry__panel { width: min(100%, 30rem); padding: 16px; justify-self: center; }
  .account-entry .account-form { gap: 12px; margin-top: 16px; }
  .account-entry__links { margin-top: 12px; }
  .account-entry__links .btn { padding-inline: 8px; }
}
@media (max-width: 380px) {
  .account-entry__intro { min-height: 36px; }
  .account-entry__panel { padding: 14px; }
  .account-entry__panel h2 { margin-bottom: 8px; }
  .account-entry__panel p { margin-block: 8px 12px; }
}
</style>
