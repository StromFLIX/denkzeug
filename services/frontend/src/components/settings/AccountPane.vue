<template>
  <div class="account-settings stack">
    <header><h2 class="settings__title">{{ title }}</h2><p class="muted">{{ subtitle }}</p></header>
    <p v-if="error" role="alert" class="account-error">{{ error }}</p>
    <p v-if="notice" role="status" class="account-notice">{{ notice }}</p>
    <section v-if="tab === 'account'" class="account-section">
      <h3>This session</h3>
      <p class="caption muted">Signed in as {{ account.user.username }} · {{ account.user.role }}</p>
      <div class="row account-session-actions">
        <button class="btn" :disabled="busy" @click="perform(() => accountApi.logout())">Sign out</button>
        <button v-if="isNative" class="btn" :disabled="busy" @click="perform(() => accountApi.switchServer(''))">Switch workspace</button>
      </div>
    </section>
    <div v-if="loading" role="status">Loading account settings…</div>
    <template v-else-if="tab === 'account'">
      <section id="account-profile" class="account-section">
        <h3>Profile</h3>
        <p class="caption muted">{{ !account.user.email ? 'No recovery email (optional)' : account.user.email_verified ? 'Recovery email verified' : 'Recovery email not verified' }}</p>
        <form class="account-form" @submit.prevent="saveProfile">
          <label>Display name<input v-model="displayName" class="field" autocomplete="name" maxlength="100" :disabled="busy" /></label>
          <label>Recovery email (optional)<input v-model="email" class="field" type="email" autocomplete="email" :disabled="busy" /></label>
          <p class="caption muted">Your username and password work without email. Leave this blank to use administrator-issued reset codes instead.</p>
          <template v-if="email !== (account.user.email || '')">
            <p class="caption muted">Confirm your password to add, change or remove this optional address. Email recovery is available only after verification.</p>
            <label>Current password<input v-model="emailPassword" class="field" type="password" required autocomplete="current-password" :disabled="busy" /></label>
          </template>
          <button class="btn btn--primary" :disabled="busy">Save profile</button>
          <button v-if="account.user.email && !account.user.email_verified && meta.mail_available" type="button" class="btn" :disabled="busy" @click="perform(() => accountApi.requestVerification(), 'Verification link queued. Your account works without verifying email.')">Verify recovery email</button>
        </form>
      </section>
      <section id="account-password" class="account-section">
        <h3>Password</h3>
        <form class="account-form" @submit.prevent="changePassword">
          <label>Current password<input v-model="currentPassword" class="field" type="password" required autocomplete="current-password" :disabled="busy" /></label>
          <label>New password<input v-model="password" class="field" type="password" required minlength="15" autocomplete="new-password" :disabled="busy" /></label>
          <p class="caption muted">Use at least 15 characters and a password you don’t use elsewhere.</p>
          <label>Confirm new password<input v-model="confirmation" class="field" type="password" required autocomplete="new-password" :disabled="busy" /></label>
          <button class="btn btn--primary" :disabled="busy">Change password</button>
        </form>
      </section>
      <section id="account-sessions" class="account-section">
        <h3>Devices & sessions</h3>
        <p class="muted caption">End sessions you no longer recognize. Signing out here also closes your private workspace on this device.</p>
        <ul class="account-list"><li v-for="session in sessions" :key="session.id"><div class="grow"><strong>{{ session.device || 'Browser or app' }}{{ session.current ? ' (this session)' : '' }}</strong><p class="caption muted">Last active {{ date(session.last_seen_at) }} · Expires {{ date(session.expires_at) }}</p></div><button class="btn" :disabled="busy" @click="endSession(session)">End session</button></li></ul>
        <button class="btn" :disabled="busy" @click="perform(() => accountApi.logout(true))">Sign out all devices</button>
      </section>
    </template>
    <template v-else-if="isAdmin && tab === 'users'">
      <section id="account-users" class="account-section">
        <p class="muted">Administrators manage accounts. Conversations stay private. Recovery codes let their recipient replace a password, expire after 30 minutes and are audited.</p>
        <div v-if="recovery" class="account-notice" role="status"><h3>Password recovery for @{{ recovery.username }}</h3><p>Share this code securely with this person only. It expires {{ date(recovery.expires_at) }} and can be used once.</p><label>Password reset code<input class="field" readonly :value="recovery.code" @focus="$event.target.select()" /></label><button class="btn" @click="perform(() => copyText(recovery.code), 'Reset code copied.')">Copy reset code</button><button v-if="recovery.link" class="btn" @click="perform(() => copyText(recovery.link), 'Reset link copied.')">Copy reset link</button><button class="btn" @click="recovery = null">Hide reset code</button></div>
        <p v-if="!users.length">No accounts found.</p>
        <ul class="account-list"><li v-for="user in users" :key="user.id"><div class="grow"><strong>{{ user.display_name || user.username }}</strong><p class="caption muted">@{{ user.username }} · {{ user.email || 'No recovery email' }} · {{ user.status }}{{ user.id === account.user.id ? ' · You' : '' }}</p></div><label class="account-inline">Role<select class="field" :value="user.role" :disabled="busy" @change="updateUser(user, { role: $event.target.value }, $event)"><option value="member">Member</option><option value="admin">Administrator</option></select></label><button v-if="user.status === 'active'" class="btn" :disabled="busy" @click="createRecovery(user)">Create reset code</button><button class="btn" :disabled="busy" @click="updateUser(user, { status: user.status === 'active' ? 'suspended' : 'active' })">{{ user.status === 'active' ? 'Suspend' : 'Reactivate' }}</button></li></ul>
      </section>
    </template>
    <template v-else-if="isAdmin && tab === 'invitations'">
      <section id="account-invitations" class="account-section">
        <form class="account-form" @submit.prevent="createInvitation">
          <p class="muted">Create a code, copy it and share it however you like. No email address is required.</p>
          <label>Note (optional)<input v-model="inviteLabel" class="field" maxlength="100" placeholder="For Sam" autocomplete="off" :disabled="busy" /></label>
          <label>Send to email (optional)<input v-model="inviteEmail" class="field" type="email" autocomplete="off" :disabled="busy" /></label>
          <label>Role<select v-model="inviteRole" class="field" :disabled="busy"><option value="member">Member</option><option value="admin">Administrator</option></select></label>
          <label v-if="policy.mail_available && inviteEmail.trim()" class="account-checkbox"><input v-model="sendEmail" type="checkbox" /> Send invitation by email</label>
          <p v-else class="muted caption">Copy and share the invitation code. Email is completely optional.</p>
          <p class="caption muted">Invitations expire after 7 days and can only be used once. {{ policy.registration_mode === 'paused' ? 'Registration is currently paused. Enable invitation-only access in Access policies before inviting.' : '' }}</p>
          <button class="btn btn--primary" :disabled="busy || policy.registration_mode === 'paused'">Create invitation</button>
        </form>
        <div v-if="created" class="account-notice" role="status"><h3>Invitation created</h3><p>Copy it now. The secret code is not shown again after you leave this page.</p><label v-if="created.link">Invitation link<input class="field" readonly :value="created.link" aria-label="Invitation link" @focus="$event.target.select()" /></label><label>Invitation code<input class="field" readonly :value="created.code" aria-label="Invitation code" @focus="$event.target.select()" /></label><button class="btn btn--primary" @click="perform(() => copyText(created.code), 'Invitation code copied.')">Copy code</button><button v-if="created.link" class="btn" @click="copyInvitation">Copy link</button><button class="btn" @click="created = null">Hide secret</button></div>
        <ul class="account-list"><li v-for="invite in invitations" :key="invite.id"><div class="grow"><strong>{{ invite.label || invite.email || `Code-only invitation · ${invite.id.slice(0, 6)}` }}</strong><p class="caption muted">{{ invite.role }} · {{ invite.status || 'pending' }} · Expires {{ date(invite.expires_at) }}</p></div><button v-if="!invite.status || ['pending', 'active'].includes(invite.status)" class="btn" :disabled="busy" @click="perform(async () => { await accountApi.revokeInvitation(invite.id); await load() }, 'Invitation revoked.')">Revoke</button></li></ul>
      </section>
    </template>
    <template v-else-if="isAdmin && tab === 'access'">
      <section id="account-policy" class="account-section">
        <form class="account-form" @submit.prevent="perform(async () => { policy = await accountApi.savePolicy({ registration_mode: registrationMode }); await load() }, 'Access policy saved.')">
          <label>Registration<select v-model="registrationMode" aria-label="Registration" class="field" :disabled="busy"><option value="invite_only">Invitation only</option><option value="paused">Paused</option></select></label>
          <p class="muted">There is no public sign-up. Invitation only lets invited people join. Paused stops new registrations; existing accounts can still sign in.</p>
          <button class="btn btn--primary" :disabled="busy">Save access policy</button>
        </form>
        <h3>Email delivery</h3><p>{{ policy.mail_available ? 'Configured. Invitations, email verification and password resets are available.' : 'Not configured—and not required. Copy/paste invitation codes and administrator-issued reset codes work without email.' }}</p>
        <AccessSharing />
        <h3>Identity providers</h3><p class="muted">This workspace uses account passwords. External identity providers are not enabled.</p>
      </section>
    </template>
  </div>
</template>
<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { account, clearAccount, isNative } from '../../auth-session.js'
import { accountApi } from '../../account.js'
import { copyText } from '../../clipboard.js'
import AccessSharing from './AccessSharing.vue'
const route = useRoute(), tab = computed(() => route.params.tab || 'account'), isAdmin = computed(() => account.user?.role === 'admin')
const titles = { account: 'Profile & security', users: 'Users', invitations: 'Invitations', access: 'Access policies' }
const title = computed(() => titles[tab.value]), subtitle = computed(() => tab.value === 'account' ? 'Your identity, password and signed-in devices.' : 'Manage who can join and use this workspace.')
const meta = ref({}), recovery = ref(null)
const loading = ref(true), busy = ref(false), error = ref(''), notice = ref(''), sessions = ref([]), users = ref([]), invitations = ref([]), policy = ref({}), registrationMode = ref('paused')
const displayName = ref(account.user.display_name || ''), email = ref(account.user.email || ''), emailPassword = ref(''), currentPassword = ref(''), password = ref(''), confirmation = ref('')
const inviteEmail = ref(''), inviteLabel = ref(''), inviteRole = ref('member'), sendEmail = ref(false), created = ref(null)
const date = value => value ? new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value).toLocaleString() : 'Unknown'
async function load () {
  if (tab.value === 'account') { sessions.value = (await accountApi.sessions()).sessions; meta.value = await accountApi.meta() }
  else if (isAdmin.value && tab.value === 'users') users.value = (await accountApi.users()).users
  else if (isAdmin.value) {
    policy.value = await accountApi.policy(); registrationMode.value = policy.value.registration_mode
    if (tab.value === 'invitations') invitations.value = (await accountApi.invitations()).invitations
  }
}
async function perform (task, success = '') {
  if (busy.value) return
  busy.value = true; error.value = notice.value = ''
  try { const result = await task(); notice.value = success; return result } catch (e) { error.value = e.message } finally { busy.value = false }
}
function saveProfile () { return perform(async () => { const body = { display_name: displayName.value }; if (email.value !== (account.user.email || '')) Object.assign(body, { email: email.value, current_password: emailPassword.value }); await accountApi.profile(body); emailPassword.value = ''; displayName.value = account.user.display_name || ''; email.value = account.user.email || '' }, 'Profile saved. Recovery email is optional; verify it to enable email recovery.') }
function changePassword () {
  if (password.value !== confirmation.value) { error.value = 'Passwords do not match.'; return }
  return perform(async () => { await accountApi.changePassword({ current_password: currentPassword.value, password: password.value }); currentPassword.value = password.value = confirmation.value = '' }, 'Password changed. Sign in again.')
}
function endSession (session) { return perform(async () => { await accountApi.revokeSession(session.id); if (session.current) clearAccount(); else await load() }, 'Session ended.') }
async function updateUser (user, body, event) {
  if (event) event.target.value = user.role
  if (body.role === 'admin' && user.role !== 'admin' && !window.confirm(`Give @${user.username} administrator access? Administrators manage accounts and workspace infrastructure.`)) return
  const result = await perform(async () => { const changed = await accountApi.updateUser(user.id, body); await load(); if (user.id === account.user.id) await accountApi.me(); return changed }, 'Account updated.')
  if (result?.automation_pause_pending) notice.value = 'Account access updated. Automation pause is pending; the backend will retry.'
}
function createRecovery (user) { return perform(async () => { recovery.value = { ...await accountApi.recoveryCode(user.id), username: user.username } }) }
function createInvitation () { if (inviteRole.value === 'admin' && !window.confirm('Create an administrator invitation? Anyone you give this single-use code to can manage accounts and workspace infrastructure.')) return; return perform(async () => { created.value = await accountApi.invite({ label: inviteLabel.value.trim(), ...(inviteEmail.value.trim() ? { email: inviteEmail.value.trim() } : {}), role: inviteRole.value, expires_in_days: 7, send_email: Boolean(policy.value.mail_available && inviteEmail.value.trim() && sendEmail.value) }); inviteEmail.value = inviteLabel.value = ''; await load() }) }
function copyInvitation () { return perform(async () => { await copyText(created.value.link) }, 'Invitation link copied.') }
onMounted(async () => { try { await load() } catch (e) { error.value = e.message } finally { loading.value = false } })
</script>
<style scoped>
.account-settings { max-width: 48rem; gap: 1.5rem; }
.account-section { border-top: 1px solid var(--border); padding-top: 1.5rem; }
.account-section h3 { font-size: 1rem; margin: 0 0 .75rem; font-weight: 600; }
.account-session-actions { flex-wrap: wrap; margin-top: .75rem; }
.account-section .account-form { max-width: 27rem; margin: 1rem 0 1.5rem; }
.account-section .account-form .btn { justify-self: start; }
.account-list { list-style: none; padding: 0; margin: 1rem 0; }
.account-list li { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem; padding: 1rem 0; border-bottom: 1px solid var(--border); }
.account-list .grow { min-width: 12rem; overflow-wrap: anywhere; }
.account-list p { margin: .4rem 0 0; }
.account-inline { display: grid; gap: .3rem; font-size: .75rem; }
.account-inline select { min-height: 44px; font-size: max(16px, .875rem); }
.account-form .account-checkbox { display: flex; align-items: center; }
.account-checkbox input { width: 1rem; }
.account-settings .btn { min-height: 44px; touch-action: manipulation; }
.account-notice label { display: grid; gap: .5rem; margin-bottom: .75rem; }
</style>
