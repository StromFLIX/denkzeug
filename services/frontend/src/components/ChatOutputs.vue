<template>
  <section class="chat-outputs scroll-y grow" aria-label="Chat workspace">
    <div class="chat-outputs__inner">
      <div v-if="!outputs.length" class="chat-outputs__empty">
        <span class="material-icons" aria-hidden="true">inventory_2</span>
        <h2>No outputs yet</h2>
        <p class="dim">Generated files and preview images will appear here when the agent publishes them. You can close the chat without interrupting its work.</p>
      </div>
      <template v-else>
        <div class="workspace-heading">
          <div><h2>Published outputs</h2><p class="caption dim">{{ outputs.length }} {{ outputs.length === 1 ? 'output' : 'outputs' }} in this chat</p></div>
          <div class="workspace-actions">
            <button type="button" class="btn btn--sm btn--outline" @click="browsing = true">Browse outputs</button>
            <button type="button" class="btn btn--sm btn--outline" :aria-pressed="canvas" @click="canvas ? showDetail() : showCanvas()">{{ canvas ? 'Output details' : 'Open canvas' }}</button>
          </div>
        </div>
        <q-dialog v-model="browsing" position="bottom" :full-width="$q.screen.width <= 650" aria-labelledby="workspace-picker-title" @hide="focusPickedOutput">
          <div class="workspace-picker">
            <header><h2 id="workspace-picker-title">Published outputs</h2><button v-close-popup type="button" class="btn btn--sm btn--outline">Done</button></header>
            <button v-for="output in outputs" :key="output.id" type="button" class="workspace-picker__item" :aria-current="output.id === selected?.id ? 'true' : undefined" @click="openOutput(output.id)">
              <span class="material-icons" aria-hidden="true">description</span>
              <span><strong>{{ output.title }}</strong><small>Revision {{ output.revision }} · {{ output.files.length }} {{ output.files.length === 1 ? 'file' : 'files' }}</small></span>
            </button>
          </div>
        </q-dialog>
        <div class="workspace-body"><div class="workspace-grid" :class="{ 'workspace-grid--indexed': outputs.length > 1 }">
          <nav v-if="outputs.length > 1" class="workspace-index" aria-label="Output index">
            <button v-for="output in outputs" :key="output.id" type="button" class="workspace-picker__item" :aria-current="!canvas && output.id === selected?.id ? 'true' : undefined" @click="openOutput(output.id)">
              <span class="material-icons" aria-hidden="true">description</span>
              <span><strong>{{ output.title }}</strong><small>Revision {{ output.revision }} · {{ output.files.length }} {{ output.files.length === 1 ? 'file' : 'files' }}</small></span>
            </button>
          </nav>
          <div class="workspace-content">
            <OutputWorkspaceCanvas v-if="visitedCanvas" v-show="canvas" :chat-id="chatId" :outputs="outputs" @open="openOutput" />
            <div v-if="!canvas">
              <div class="chat-outputs__controls">
                <label>Revision
                  <select v-model="revision">
                    <option :value="null">Latest · {{ selected?.revision }}</option>
                    <option v-for="item in history.filter(item => item.revision !== selected?.revision)" :key="item.revision" :value="item.revision">Revision {{ item.revision }}</option>
                  </select>
                </label>
                <button v-if="selected?.revision > 1" type="button" class="btn btn--sm btn--outline" :disabled="loading" @click="loadHistory">{{ loading ? 'Loading…' : 'Load revision history' }}</button>
              </div>
              <p class="caption dim" role="status">{{ revision === null ? 'Showing the latest published snapshot. New revisions update here automatically.' : 'Viewing a saved revision. Select Latest to follow new results.' }}</p>
              <p v-if="working" class="caption dim" role="status">The agent is working. Only committed outputs are shown.</p>
              <p v-if="error" class="caption" role="alert">{{ error }}</p>
              <OutputCard v-if="displayed" ref="detail" :chat-id="chatId" :output="displayed" />
            </div>
          </div>
        </div></div>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import OutputCard from './OutputCard.vue'
import OutputWorkspaceCanvas from './OutputWorkspaceCanvas.vue'
import { api } from '../api'
import { onLiveEvent } from '../store'

const props = defineProps({
  chatId: { type: String, required: true },
  outputs: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
  selectedRevision: { type: Number, default: null },
  openRequest: { type: Number, default: 0 },
  working: { type: Boolean, default: false }
})
const emit = defineEmits(['update:selectedId', 'update:selectedRevision'])
const selected = computed(() => props.outputs.find(output => output.id === props.selectedId) || props.outputs[0])
const revision = computed({ get: () => props.selectedRevision, set: value => emit('update:selectedRevision', value) })
const canvas = ref(!props.selectedId && props.outputs.length > 1)
const visitedCanvas = ref(canvas.value)
const browsing = ref(false)
const detail = ref(null)
let focusAfterPicker = false
function focusPickedOutput () {
  if (!focusAfterPicker) return
  focusAfterPicker = false
  detail.value?.focusHeading()
}
const history = ref([])
const loading = ref(false)
const error = ref('')
const displayed = computed(() => revision.value === null ? selected.value : history.value.find(item => item.revision === revision.value))
let controller
function showCanvas () { visitedCanvas.value = true; canvas.value = true }
function showDetail () { canvas.value = false }
function selectOutput (id) {
  emit('update:selectedRevision', null)
  emit('update:selectedId', id)
}
async function openOutput (id) {
  if (id !== selected.value?.id) selectOutput(id)
  else if (!props.selectedId) emit('update:selectedId', id)
  focusAfterPicker = browsing.value
  browsing.value = false
  showDetail()
  if (!focusAfterPicker) await nextTick(() => detail.value?.focusHeading())
}
watch(() => props.openRequest, showDetail)
watch([() => selected.value?.id, revision], ([id, rev], [previousId] = []) => {
  if (id !== previousId) {
    controller?.abort()
    history.value = []
    loading.value = false
    error.value = ''
  }
  // Keep the output pinned by identity, not by its position after a publication.
  if (id && id !== props.selectedId) selectOutput(id)
  else if (id && rev !== null && !history.value.some(item => item.revision === rev)) loadHistory()
}, { immediate: true })
async function loadHistory () {
  const id = selected.value?.id
  if (!id) return
  controller?.abort()
  const request = new AbortController()
  controller = request
  loading.value = true
  error.value = ''
  try {
    const data = await api.outputRevisions(props.chatId, id, AbortSignal.any([request.signal, AbortSignal.timeout(15000)]))
    if (!request.signal.aborted && selected.value?.id === id) {
      history.value = data.revisions
      if (revision.value !== null && !data.revisions.some(item => item.revision === revision.value)) {
        revision.value = null
        error.value = 'That revision was removed in Storage. Showing the latest retained snapshot.'
      }
    }
  } catch (err) {
    if (!request.signal.aborted) error.value = `Could not load revisions: ${err.message}. Try again.`
  } finally {
    if (!request.signal.aborted) loading.value = false
  }
}
const unsubscribe = onLiveEvent(event => {
  if (event.kind === 'chat.output.deleted' && event.chat_id === props.chatId && selected.value) {
    history.value = []
    loadHistory()
  }
})
onUnmounted(() => { controller?.abort(); unsubscribe() })
</script>

<style scoped>
.chat-outputs { min-height: 0; padding: 18px 24px; }
.chat-outputs__inner { max-width: calc(var(--content-width) + 48px); margin: 0 auto; }
.chat-outputs__controls { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.chat-outputs__controls label { display: flex; flex-direction: column; gap: 5px; min-width: 0; font-size: 0.75rem; color: var(--text-muted); }
.workspace-body { container-type: inline-size; }
.workspace-index { display: none; }
.workspace-content { min-width: 0; }
@container (min-width: 760px) {
  .workspace-grid--indexed { display: grid; grid-template-columns: min(225px, 30%) minmax(0, 1fr); gap: 12px; }
  .workspace-index { display: block; padding-right: 8px; border-right: 1px solid var(--border); }
}
.workspace-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
.workspace-heading h2, .workspace-picker h2 { margin: 0; font-size: 0.9rem; }
.workspace-heading p { margin: 4px 0 0; }
.workspace-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.workspace-actions button, .workspace-picker button, .chat-outputs__controls button { min-height: 44px; }
.workspace-picker { width: min(100%, 560px); max-height: 85dvh; padding: 10px 12px max(16px, env(safe-area-inset-bottom)); background: var(--surface-panel); color: var(--text); overflow: auto; }
.workspace-picker > header { position: sticky; top: -10px; z-index: 1; background: var(--surface-panel); display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 0 2px 8px; }
.workspace-picker__item { display: flex; align-items: center; gap: 12px; width: 100%; padding: 9px; color: var(--text); background: transparent; border: 1px solid transparent; border-radius: var(--radius-sm); text-align: left; font: inherit; overflow-wrap: anywhere; }
.workspace-picker__item[aria-current], .workspace-picker__item:hover { background: var(--surface-active); border-color: var(--border); }
.workspace-picker__item small { display: block; font-size: 0.7rem; color: var(--text-muted); }
.chat-outputs__controls select { max-width: 100%; min-height: 44px; padding: 8px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-input); color: var(--text); font: inherit; font-size: 0.875rem; }
.chat-outputs__empty { max-width: 460px; margin: 60px auto; text-align: center; }
.chat-outputs__empty > .material-icons { font-size: 2rem; color: var(--accent); }
.chat-outputs__empty h2 { font-size: 1.125rem; }
@media (max-width: 900px) {
  .chat-outputs { padding: 12px max(12px, env(safe-area-inset-right)) 12px max(12px, env(safe-area-inset-left)); }
}
</style>
