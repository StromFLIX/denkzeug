<template>
  <details class="settings-disclosure dream" @toggle="onToggle">
    <summary>
      <span class="grow">Dreaming</span>
      <span v-if="saved" class="caption dim">{{ saved.schedule ? 'On' : 'Off' }}</span>
    </summary>
    <form class="dream__body" aria-label="Dreaming settings" @submit.prevent="save">
      <p v-if="loading" class="caption dim" role="status">Loading dream…</p>
      <p v-else-if="unavailable" class="caption dim">Only this agent’s owner can review or enable dreaming.</p>
      <template v-else-if="saved">
        <label class="dream__check">
          <input v-model="enabled" type="checkbox" :disabled="busy || disabled" aria-describedby="dream-help" @change="notice = ''" />
          Reflect each night
        </label>
        <p id="dream-help" class="caption dim">The agent’s model uses excerpts from your chats, its system prompt and the previous dream to update these notes. While dreaming is on, Dreams.md gives the agent private context when you send a message.</p>
        <div v-if="enabled" class="dream__schedule">
          <label>
            Nightly time
            <input v-model="at" class="field" type="time" :disabled="busy || disabled" @input="notice = ''" />
          </label>
          <label>
            Timezone
            <input v-model="timezone" class="field" type="text" maxlength="100" placeholder="e.g. Europe/Berlin" :disabled="busy || disabled" @input="notice = ''" />
          </label>
        </div>
        <label for="agent-dream-text" class="dream__label">Latest dream (Dreams.md)</label>
        <p v-if="saved.last_status" id="dream-status" class="caption dim">{{ lastRun }}</p>
        <textarea id="agent-dream-text" v-model="text" class="field dream__text" rows="7" :maxlength="saved.max_chars" :aria-describedby="saved.last_status ? 'dream-status' : undefined" :disabled="busy || disabled" placeholder="No dream yet. You can add notes here, or wait for the first night with chat activity." @input="notice = ''" />
        <p class="caption dim">The next dream can revise these notes. Turning dreaming off stops using them but keeps the text. Empty this field to clear it.</p>
        <div class="dream__actions">
          <button class="btn btn--sm btn--outline" type="submit" :disabled="busy || disabled || !canSave">{{ busy ? 'Saving…' : 'Save dreaming' }}</button>
          <span v-if="notice" class="caption dim" role="status">{{ notice }}</span>
        </div>
      </template>
      <p v-if="error" class="caption field-hint--bad" role="alert">{{ error }}</p>
      <button v-if="reloadNeeded" class="btn btn--sm" type="button" :disabled="loading || disabled" @click="load">{{ conflict ? 'Reload saved dream' : 'Retry loading dream' }}</button>
    </form>
  </details>
</template>

<script setup>
import { computed, onUnmounted, ref } from 'vue'
import { api } from '../../api'

const props = defineProps({ id: { type: String, required: true }, disabled: Boolean })
const emit = defineEmits(['busy'])
const saved = ref(null), loading = ref(false), busy = ref(false), unavailable = ref(false), conflict = ref(false)
const enabled = ref(false), at = ref(''), timezone = ref(''), text = ref(''), error = ref(''), notice = ref('')
const validTime = /^([01]\d|2[0-3]):[0-5]\d$/
const schedule = computed(() => enabled.value ? { at: at.value, timezone: timezone.value.trim() } : null)
const dirty = computed(() => saved.value && (
  schedule.value?.at !== saved.value.schedule?.at || schedule.value?.timezone !== saved.value.schedule?.timezone ||
  text.value !== saved.value.dream
))
const canSave = computed(() => dirty.value && !conflict.value &&
  (!enabled.value || (validTime.test(at.value) && timezone.value.trim())))
const reloadNeeded = computed(() => !loading.value && !unavailable.value && (conflict.value || (!saved.value && error.value)))
const lastRun = computed(() => {
  const descriptions = { updated: 'Last generated', no_activity: 'No eligible chat activity', failed: 'Update failed; your previous dream was kept' }
  const timestamp = saved.value.last_attempt_at
  return `${descriptions[saved.value.last_status]}${timestamp ? ` · ${new Date(timestamp * 1000).toLocaleString()}` : ''}`
})

function apply (data) {
  saved.value = data
  enabled.value = Boolean(data.schedule)
  at.value = data.schedule?.at || ''
  timezone.value = data.schedule?.timezone || ''
  text.value = data.dream
}
async function load () {
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    apply(await api.agentDream(props.id))
    conflict.value = false
  } catch (failure) {
    if (failure.status === 404) unavailable.value = true
    else error.value = failure.message
  } finally { loading.value = false }
}
function onToggle (event) {
  if (event.currentTarget.open && !dirty.value && !busy.value && !loading.value && !unavailable.value) load()
}
async function save () {
  if (!canSave.value || busy.value || props.disabled) return
  busy.value = true
  emit('busy', true)
  error.value = ''
  notice.value = ''
  try {
    const payload = { revision: saved.value.revision, schedule: schedule.value }
    if (text.value !== saved.value.dream) payload.dream = text.value
    apply(await api.saveAgentDream(props.id, payload))
    notice.value = 'Dreaming saved.'
  } catch (failure) {
    if (failure.status === 409) conflict.value = true
    error.value = failure.message
  } finally {
    busy.value = false
    emit('busy', false)
  }
}
onUnmounted(() => emit('busy', false))
</script>

<style scoped>
.dream { margin: 20px 0; }
.dream__body { display: grid; gap: 8px; padding: 12px 16px 16px; }
.dream__body p { margin: 0; max-width: 70ch; }
.dream__check { display: flex; align-items: center; gap: 8px; min-height: 36px; font-size: .8125rem; }
.dream__check input { margin: 0; }
.dream__schedule { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: 8px; margin: 4px 0; }
.dream__schedule label, .dream__label { display: grid; gap: 6px; font-size: .75rem; }
.dream__schedule .field, .dream__text { min-width: 0; width: 100%; }
.dream__label { margin-top: 8px; }
.dream__text { min-height: 8rem; height: clamp(8rem, 25vh, 12rem); resize: vertical; line-height: 1.6; }
.dream__actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.dream__body > .btn { justify-self: start; }
@media (max-width: 600px) {
  .dream__schedule { grid-template-columns: minmax(0, 1fr); }
  .dream__text { height: 8rem; }
}
@media (pointer: coarse) { .dream__check, .dream__actions .btn { min-height: 44px; } }
</style>
