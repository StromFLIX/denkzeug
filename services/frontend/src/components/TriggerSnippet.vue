<template>
  <div class="trigger">
    <h4>Webhook trigger</h4>
    <p>Create a credential that can run only this workflow. Anyone with it can trigger the workflow, so share it securely.</p>
    <div class="trigger__controls">
      <label>Link expires in<select v-model="days" class="field" :disabled="busy"><option :value="1">1 day</option><option :value="3">3 days</option><option :value="7">7 days</option><option :value="null">Never expires</option></select></label>
      <button class="btn" :disabled="busy" @click="createAndCopy(true)"><span class="material-icons" aria-hidden="true">link</span>Copy webhook URL</button>
    </div>
    <p v-if="days === null" class="caption trigger__warning">This credential never expires. Revoke it manually when it is no longer needed or if it is exposed. Your account’s permissions still apply.</p>
    <p class="muted caption">Each copy creates a new token. Previous credentials keep working until they expire or you revoke them.</p>
    <div class="trigger__tabs" role="group" aria-label="Integration format">
      <button v-for="option in triggerLanguages" :key="option" class="tab" :class="{ 'tab--active': language === option }" :aria-pressed="language === option" :disabled="busy" @click="language = option">{{ option }}</button>
      <button class="btn btn--sm btn--primary trigger__copy" :disabled="busy" @click="createAndCopy(false)"><span class="material-icons" aria-hidden="true">content_copy</span>{{ busy ? 'Creating…' : 'Copy' }}</button>
    </div>
    <p class="muted caption">{{ language === 'GET' ? 'GET link — input travels as URL-encoded JSON. Opening the copied link runs the workflow.' : 'POST request — input is sent as JSON.' }} The preview uses a placeholder; Copy creates a dedicated workflow token. Account sessions are never included.</p>
    <pre class="trigger__code mono" aria-label="Integration preview">{{ preview }}</pre>
    <p v-if="error" role="alert" class="trigger__error">{{ error }}</p>
    <p v-if="created" class="caption" role="status">{{ copied ? `${copiedLabel} copied.` : 'Workflow token created. Copy from the dialog.' }} {{ tokenExpiry(created.expires_at) }}. <RouterLink :to="{ path: '/settings/api-keys', query: { token: created.id } }">Manage or revoke this token</RouterLink></p>
    <TokenSecretDialog :value="reveal" @close="reveal = null" />
  </div>
</template>
<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { api, server } from '../api.js'
import { copyText } from '../clipboard.js'
import { tokenExpiry } from '../api-keys.js'
import { triggerLanguages, triggerPreviewUrl, triggerSnippet } from '../trigger-snippet.js'
import TokenSecretDialog from './TokenSecretDialog.vue'
const props = defineProps({ workflow: { type: String, required: true }, inputs: { type: Object, default: () => ({}) } })
const days = ref(7), language = ref('curl'), busy = ref(false), error = ref(''), created = ref(null), reveal = ref(null), copied = ref(false), copiedLabel = ref('')
const preview = computed(() => triggerSnippet(language.value, triggerPreviewUrl(server.url || window.location.origin, props.workflow), props.inputs))
let version = 0
watch(() => props.workflow, () => { ++version; reveal.value = created.value = null; copied.value = false; error.value = ''; days.value = 7; language.value = 'curl' })
onUnmounted(() => { ++version; reveal.value = created.value = null })
async function createAndCopy (urlOnly) {
  if (busy.value) return
  const requestVersion = version, format = language.value, inputs = JSON.parse(JSON.stringify(props.inputs))
  busy.value = true; error.value = ''; copied.value = false; reveal.value = null
  try {
    const result = await api.workflowWebhookToken(props.workflow, days.value)
    if (requestVersion !== version) return
    created.value = { id: result.token.id, expires_at: result.expires_at }
    const text = urlOnly ? result.url : triggerSnippet(format, result.url, inputs)
    copiedLabel.value = urlOnly ? 'Webhook URL' : 'Integration snippet'
    // Only the one-time dialog holds the secret; previews always use placeholders.
    reveal.value = urlOnly ? result : { ...result, snippet: text }
    try { await copyText(text); if (requestVersion === version) copied.value = true }
    catch { if (requestVersion === version) error.value = 'The token was created, but the clipboard is unavailable. Copy from the dialog; no extra token is needed.' }
  } catch (failure) { if (requestVersion === version) error.value = failure.message }
  finally { busy.value = false }
}
</script>
<style scoped>
.trigger { min-width: 0; padding: 1rem; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-panel); }
.trigger h4 { margin: 0 0 .75rem; font-size: 1rem; font-weight: 600; }
.trigger p { line-height: 1.6; max-width: 48rem; }
.trigger__controls { display: flex; align-items: end; gap: .75rem; flex-wrap: wrap; }
.trigger__controls label { display: grid; gap: .4rem; font-size: .875rem; }
.trigger__controls .field { min-height: 44px; font-size: max(16px, .875rem); }
.trigger .btn, .trigger .tab { min-height: 44px; }
.trigger__tabs { display: flex; flex-wrap: wrap; gap: .25rem; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: .5rem; }
.trigger__tabs .tab { padding: .5rem .75rem; }
.trigger__copy { margin-left: auto; }
.trigger__error, .trigger__warning { color: var(--danger); }
.trigger__code { max-width: 100%; white-space: pre; overflow-x: auto; padding: 1rem; background: var(--surface-code); line-height: 1.6; }
@media (max-width: 420px) {
  .trigger { padding: .75rem; }
  .trigger__tabs .tab { padding: .5rem; }
  .trigger__copy { flex-basis: 100%; }
}
</style>
