<template>
  <section class="chat-outputs scroll-y grow">
    <div v-if="!outputs.length" class="chat-outputs__empty">
      <span class="material-icons" aria-hidden="true">inventory_2</span>
      <h2 ref="heading" tabindex="-1">No outputs yet</h2>
      <p class="dim">Files the agent publishes in this chat will appear here.</p>
    </div>
    <div v-else class="chat-outputs__inner">
      <header class="workspace-heading">
        <h2 ref="heading" tabindex="-1">Published outputs</h2>
        <p class="caption dim">{{ countLabel(outputs.length, 'output') }}</p>
      </header>
      <ul class="workspace-gallery" aria-label="Published outputs">
        <li v-for="{ output, preview, file } in tiles" :key="output.id">
          <button :ref="element => setTile(output.id, element)" type="button" class="workspace-tile"
            :class="{ 'workspace-tile--file': !preview }" aria-haspopup="dialog" :aria-label="`Open ${output.title}`"
            :title="output.title" @click="openOutput(output.id)">
            <OutputImage v-if="preview" class="workspace-tile__preview" thumbnail :chat-id="chatId"
              :output-id="output.id" :revision="output.revision" :file="preview" />
            <span v-else class="workspace-tile__icon material-icons" aria-hidden="true">description</span>
            <span class="workspace-tile__body">
              <strong>{{ output.title }}</strong>
              <span v-if="!preview && file && file.name !== output.title" class="workspace-tile__filename">{{ file.name }}</span>
              <span class="workspace-tile__meta">{{ countLabel(output.files.length, 'file') }} · Revision {{ output.revision }}</span>
            </span>
            <span class="workspace-tile__open material-icons" aria-hidden="true">arrow_forward</span>
          </button>
        </li>
      </ul>
    </div>
    <q-dialog class="workspace-detail-dialog" :model-value="Boolean(selected)" :position="$q.screen.width <= 650 ? 'standard' : 'right'"
      :maximized="$q.screen.width <= 650" square aria-label="Output details" no-refocus
      @update:model-value="closeOutput" @hide="restoreFocus">
      <div class="workspace-detail">
        <header class="workspace-detail__bar">
          <button type="button" class="workspace-detail__back btn btn--sm" data-autofocus @click="closeOutput(false)">
            <span class="material-icons" aria-hidden="true">arrow_back</span>All outputs
          </button>
        </header>
        <div v-if="selected" class="workspace-detail__body scroll-y">
          <OutputCard v-if="displayed" :key="displayed.id" :chat-id="chatId" :output="displayed" embedded>
            <template #before-files>
              <OutputRevisionPicker v-model="revision" :latest="selected.revision" :prior="prior" :loading="loading" :error="error" @load="loadHistory" />
            </template>
          </OutputCard>
          <div v-else class="workspace-detail__pending">
            <h2>{{ selected.title }}</h2>
            <OutputRevisionPicker v-model="revision" :latest="selected.revision" :prior="prior" :loading="loading" :error="error" @load="loadHistory" />
            <p v-if="loading" class="caption dim" role="status">Loading revision {{ revision }}…</p>
          </div>
        </div>
      </div>
    </q-dialog>
  </section>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import OutputCard from './OutputCard.vue'
import OutputImage from './OutputImage.vue'
import OutputRevisionPicker from './OutputRevisionPicker.vue'
import { api } from '../api'
import { onLiveEvent } from '../store'

const props = defineProps({
  chatId: { type: String, required: true },
  outputs: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
  selectedRevision: { type: Number, default: null }
})
const emit = defineEmits(['update:selectedId', 'update:selectedRevision'])
const countLabel = (count, noun) => `${count} ${noun}${count === 1 ? '' : 's'}`
const tiles = computed(() => props.outputs.map(output => ({
  output,
  preview: output.files.find(file => file.role === 'preview' && file.previewable) || output.files.find(file => file.previewable),
  file: output.files.find(file => file.role === 'file') || output.files[0]
})))
const selected = computed(() => props.outputs.find(output => output.id === props.selectedId))
const revision = computed({ get: () => props.selectedRevision, set: value => emit('update:selectedRevision', value) })
const heading = ref(null)
const tileButtons = new Map()
let returnTo = ''
function setTile (id, element) {
  if (element) tileButtons.set(id, element)
  else tileButtons.delete(id)
}
function openOutput (id) {
  revision.value = null
  emit('update:selectedId', id)
}
function closeOutput (open) {
  if (open || !props.selectedId) return
  revision.value = null
  emit('update:selectedId', '')
}
function restoreFocus () {
  if (!selected.value) (tileButtons.get(returnTo) || heading.value)?.focus({ preventScroll: true })
}
const history = ref([])
const loading = ref(false)
const error = ref('')
const displayed = computed(() => revision.value === null || revision.value === selected.value?.revision
  ? selected.value : history.value.find(item => item.revision === revision.value))
const prior = computed(() => history.value.map(item => item.revision).filter(value => value !== selected.value?.revision))
let controller
watch(() => selected.value?.id, (id, previousId) => {
  controller?.abort()
  history.value = []
  loading.value = false
  error.value = ''
  if (id) returnTo = id
  else if (previousId && props.selectedId === previousId) closeOutput(false)
}, { immediate: true })
watch([() => selected.value?.id, revision], ([id, rev]) => {
  if (id && rev !== null && !displayed.value) loadHistory()
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
      if (revision.value !== null && !displayed.value) {
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
  if (event.kind === 'chat.output.deleted' && event.chat_id === props.chatId && selected.value) loadHistory()
})
onUnmounted(() => { controller?.abort(); unsubscribe() })
</script>

<style scoped>
.chat-outputs { min-height: 0; padding: clamp(18px, 3vw, 36px); background: var(--surface-app); }
.chat-outputs__inner { max-width: 68rem; margin-inline: auto; }
.workspace-heading { margin-bottom: 18px; }
.workspace-heading h2 { margin: 0; font-size: 1.25rem; line-height: 1.3; font-weight: 650; letter-spacing: -0.025em; }
.workspace-heading p { margin: 5px 0 0; }
.workspace-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(16rem, 100%), 1fr)); align-items: start; gap: 14px; list-style: none; margin: 0; padding: 0; }
.workspace-gallery li { min-width: 0; }
.workspace-tile { position: relative; display: block; width: 100%; padding: 8px; border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: var(--shadow-sm); color: var(--text); background: var(--surface-panel); font: inherit; text-align: left; cursor: pointer; }
.workspace-tile:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.workspace-tile__preview { aspect-ratio: 16 / 9; background: var(--surface-raised); border-radius: var(--radius-sm); }
.workspace-tile__body { display: grid; gap: 4px; padding: 10px 30px 5px 5px; min-width: 0; overflow-wrap: anywhere; }
.workspace-tile__body strong { font-size: 0.825rem; font-weight: 650; line-height: 1.35; }
.workspace-tile__filename, .workspace-tile__meta { color: var(--text-muted); font-size: 0.675rem; }
.workspace-tile__filename { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-tile__open { position: absolute; right: 12px; bottom: 14px; color: var(--text-muted); font-size: 18px; }
.workspace-tile--file { display: flex; gap: 11px; align-items: flex-start; padding: 13px; }
.workspace-tile--file .workspace-tile__body { padding: 0 20px 0 0; }
.workspace-tile--file .workspace-tile__open { top: 14px; bottom: auto; }
.workspace-tile__icon { display: grid; place-items: center; flex: none; width: 40px; height: 40px; border-radius: var(--radius-sm); color: var(--accent); background: var(--accent-soft); font-size: 20px; }
@media (hover: hover) and (pointer: fine) {
  .workspace-tile:hover { border-color: var(--accent); }
  .workspace-tile:hover .workspace-tile__open { color: var(--accent); }
}
.chat-outputs__empty { max-width: 28rem; margin: 60px auto; text-align: center; }
.chat-outputs__empty > .material-icons { font-size: 2rem; color: var(--accent); }
.chat-outputs__empty h2 { margin: 8px 0; font-size: 1.125rem; }
.chat-outputs__empty p { margin: 0; }
/* Draw to the viewport edges; the drawer's own header and body handle safe areas. */
:global(.workspace-detail-dialog .q-dialog__inner) { padding: 0 !important; }
:global(.workspace-detail-dialog .q-dialog__backdrop) { background: rgb(var(--shadow-color) / 0.24); }
.workspace-detail { display: flex; flex-direction: column; width: 25rem; height: 100%; max-height: var(--app-height) !important; max-width: 100vw !important; overflow: hidden; background: var(--surface-panel); color: var(--text); }
@media (max-width: 650px) { .workspace-detail { width: 100%; } }
.workspace-detail__bar { display: flex; flex: none; padding: max(8px, env(safe-area-inset-top)) max(14px, env(safe-area-inset-right)) 8px max(14px, env(safe-area-inset-left)); border-bottom: 1px solid var(--border); }
.workspace-detail__back { gap: 8px; min-height: 44px; color: var(--accent); }
.workspace-detail__back .material-icons { font-size: 18px; }
.workspace-detail__body { flex: 1; min-height: 0; padding: 20px max(18px, env(safe-area-inset-right)) max(24px, env(safe-area-inset-bottom)) max(18px, env(safe-area-inset-left)); }
.workspace-detail__pending h2 { margin: 0; overflow-wrap: anywhere; font-size: 1.125rem; }
@media (max-width: 650px) {
  .chat-outputs { padding: 16px max(12px, env(safe-area-inset-right)) 16px max(12px, env(safe-area-inset-left)); }
  .workspace-gallery { grid-template-columns: minmax(0, 1fr); }
}
</style>
