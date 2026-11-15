<template>
  <q-dialog :model-value="Boolean(value)" persistent @update:model-value="close">
    <div class="token-secret" role="region" aria-labelledby="token-secret-title">
      <span class="material-icons token-secret__icon" aria-hidden="true">key</span>
      <h2 id="token-secret-title">Save your {{ value?.snippet ? 'integration snippet' : value?.url ? 'webhook URL' : 'API key' }}</h2>
      <p>This is the only time it is shown. Store it securely; anyone with it can use its granted permissions.</p>
      <label>{{ value?.snippet ? 'Integration snippet' : value?.url ? 'Webhook URL' : 'Secret key' }}
        <textarea class="field mono" readonly :value="secretText" rows="3" autocomplete="off" spellcheck="false" @focus="$event.target.select()" />
      </label>
      <p class="caption muted">{{ tokenExpiry(expiry) }}. Closing this dialog removes the secret from this page.</p>
      <p v-if="value && expiry === null" class="caption token-secret__error">This credential never expires. Revoke it manually when it is no longer needed or if it is exposed.</p>
      <p v-if="error" role="alert" class="token-secret__error">{{ error }} Select the text and copy it manually.</p>
      <p v-if="copied" role="status" class="caption">{{ secretLabel }} copied.</p>
      <div class="token-secret__actions">
        <button class="btn btn--primary" :disabled="copying" @click="copy"><span class="material-icons" aria-hidden="true">content_copy</span>{{ copied ? 'Copy again' : value?.snippet ? 'Copy integration snippet' : value?.url ? 'Copy webhook URL' : 'Copy API key' }}</button>
        <button class="btn" @click="close">Done, hide secret</button>
      </div>
      <RouterLink v-if="value?.url" :to="{ path: '/settings/api-keys', query: { token: value.token.id } }" @click="close">Manage or revoke this token</RouterLink>
    </div>
  </q-dialog>
</template>
<script setup>
import { computed, ref, watch } from 'vue'
import { copyText } from '../clipboard.js'
import { tokenExpiry } from '../api-keys.js'
const props = defineProps({ value: { type: Object, default: null } })
const emit = defineEmits(['close'])
const copied = ref(false), copying = ref(false), error = ref('')
const secretLabel = computed(() => props.value?.snippet ? 'Integration snippet' : props.value?.url ? 'Webhook URL' : 'API key')
const secretText = computed(() => props.value?.snippet || props.value?.url || props.value?.secret)
const expiry = computed(() => props.value && Object.hasOwn(props.value, 'expires_at') ? props.value.expires_at : props.value?.token?.expires_at)
watch(() => props.value, () => { copied.value = false; error.value = '' })
function close () { emit('close') }
async function copy () {
  copying.value = true; error.value = ''
  const value = props.value
  try { await copyText(secretText.value); if (props.value === value) copied.value = true }
  catch (failure) { if (props.value === value) error.value = failure.message }
  finally { copying.value = false }
}
</script>
<style scoped>
.token-secret { width: 36rem; max-width: calc(100vw - 2rem); padding: 1.75rem; background: var(--surface-panel); color: var(--text); border-radius: var(--radius-lg); overflow-wrap: anywhere; }
.token-secret h2 { font-size: 1.35rem; font-weight: 600; margin: .5rem 0 1rem; }
.token-secret p { line-height: 1.6; }
.token-secret label { display: grid; gap: .5rem; }
.token-secret textarea { width: 100%; resize: vertical; font-size: max(16px, .875rem); }
.token-secret__icon { color: var(--accent); font-size: 2rem; }
.token-secret__actions { display: flex; flex-wrap: wrap; gap: .75rem; margin: 1rem 0; }
.token-secret .btn { min-height: 44px; max-width: 100%; white-space: normal; }
.token-secret .btn .material-icons { flex-shrink: 0; }
.token-secret__error { color: var(--danger); }
</style>
