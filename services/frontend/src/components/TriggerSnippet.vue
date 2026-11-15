<template>
  <div class="trigger">
    <h4>Webhook trigger</h4>
    <p>Create a short-lived link that can run only this workflow. Anyone with the URL can trigger it, so share it securely.</p>
    <div class="trigger__controls">
      <label>Link expires in<select v-model.number="days" class="field" :disabled="busy"><option :value="1">1 day</option><option :value="3">3 days</option><option :value="7">7 days</option></select></label>
      <button class="btn btn--primary" :disabled="busy" @click="createAndCopy"><span class="material-icons" aria-hidden="true">content_copy</span>{{ busy ? 'Creating link…' : 'Copy webhook URL' }}</button>
    </div>
    <p class="muted caption">Each copy creates a new token. Previous links keep working until they expire or you revoke them.</p>
    <p v-if="error" role="alert" class="trigger__error">{{ error }}</p>
    <p v-if="created" class="caption" role="status">{{ copied ? 'Webhook URL copied.' : 'Webhook token created. Copy the URL from the dialog.' }} Expires {{ tokenDate(created.expires_at) }}. <RouterLink :to="{ path: '/settings/api-keys', query: { token: created.id } }">Manage or revoke this token</RouterLink></p>
    <p class="muted caption">Send a POST request to the URL with this JSON input. Account sessions are never included in the link.</p>
    <pre class="trigger__code mono">{{ JSON.stringify(inputs, null, 2) }}</pre>
    <TokenSecretDialog :value="reveal" @close="reveal = null" />
  </div>
</template>
<script setup>
import { onUnmounted, ref, watch } from 'vue'
import { api } from '../api.js'
import { copyText } from '../clipboard.js'
import { tokenDate } from '../api-keys.js'
import TokenSecretDialog from './TokenSecretDialog.vue'
const props = defineProps({ workflow: { type: String, required: true }, inputs: { type: Object, default: () => ({}) } })
const days = ref(7), busy = ref(false), error = ref(''), created = ref(null), reveal = ref(null), copied = ref(false)
let version = 0
watch(() => props.workflow, () => { ++version; reveal.value = created.value = null; copied.value = false; error.value = ''; days.value = 7 })
onUnmounted(() => { ++version; reveal.value = null })
async function createAndCopy () {
  if (busy.value) return
  const requestVersion = version
  busy.value = true; error.value = ''; copied.value = false
  try {
    const result = await api.workflowWebhookToken(props.workflow, days.value)
    if (requestVersion !== version) return
    created.value = { id: result.token.id, expires_at: result.expires_at }
    // Keep the returned URL only in the one-time dialog, even if clipboard access fails.
    reveal.value = result
    try { await copyText(result.url); if (requestVersion === version) copied.value = true }
    catch { if (requestVersion === version) error.value = 'The link was created, but the clipboard is unavailable. Copy it from the dialog; no extra token is needed.' }
  } catch (failure) { if (requestVersion === version) error.value = failure.message }
  finally { busy.value = false }
}
</script>
<style scoped>
.trigger { padding: 1rem; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-panel); }
.trigger h4 { margin: 0 0 .75rem; font-size: 1rem; font-weight: 600; }
.trigger p { line-height: 1.6; max-width: 48rem; }
.trigger__controls { display: flex; align-items: end; gap: .75rem; flex-wrap: wrap; }
.trigger__controls label { display: grid; gap: .4rem; font-size: .875rem; }
.trigger__controls .field { min-height: 44px; font-size: max(16px, .875rem); }
.trigger__controls .btn { min-height: 44px; }
.trigger__error { color: var(--danger); }
.trigger__code { white-space: pre; overflow-x: auto; padding: 1rem; background: var(--surface-code); }
</style>
