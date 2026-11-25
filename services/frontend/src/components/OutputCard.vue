<template>
  <article class="output-card" :class="{ 'output-card--embedded': embedded }" :aria-label="output.title">
    <header class="output-card__head">
      <span class="material-icons" aria-hidden="true">inventory_2</span>
      <div class="grow">
        <h2>{{ output.title }}</h2>
        <p class="caption dim">Ready · Revision {{ output.revision }}<template v-if="output.source_app"> · {{ output.source_app }}</template></p>
      </div>
    </header>
    <p v-if="output.summary" class="output-card__summary">{{ output.summary }}</p>
    <div v-if="previews.length" class="output-card__previews">
      <OutputImage v-for="file in previews" :key="file.id" :chat-id="chatId" :output-id="output.id" :revision="output.revision" :file="file" />
    </div>
    <slot name="before-files" />
    <h3 v-if="embedded" class="output-card__files-heading">Files</h3>
    <ul class="output-card__files" aria-label="Output files">
      <li v-for="file in output.files" :key="file.id" class="output-card__file">
        <button type="button" class="output-card__download" :disabled="Boolean(loadingFile)" :aria-label="`Download ${file.name}`" @click="download(file)">
          <span class="material-icons" aria-hidden="true">{{ file.role === 'source' ? 'edit_document' : 'download' }}</span>
          <span class="output-card__file-details"><strong>{{ file.name }}</strong><span class="caption dim">{{ file.role === 'source' ? 'Editable source · ' : file.role === 'preview' ? 'Preview · ' : '' }}{{ formatSize(file.size) }} · {{ loadingFile === file.id ? 'Downloading…' : 'Download' }}</span></span>
        </button>
        <button type="button" class="btn btn--sm btn--outline output-card__open" :aria-label="`Open ${file.name}`" @click="openedFile = file.id">Open</button>
      </li>
    </ul>
    <q-dialog v-model="viewerOpen" :maximized="$q.screen.width <= 650 || presenting" :persistent="presenting">
      <WorkspaceFileViewer v-if="activeFile" :chat-id="chatId" :output="output" :file="activeFile" :presenting="presenting"
        :downloading="Boolean(loadingFile)" :download-error="error" :download-status="status"
        @select-file="selectFile" @update:presenting="presenting = $event" @close="viewerOpen = false" @download="download" />
    </q-dialog>
    <p v-if="error && !viewerOpen" class="caption" role="alert">{{ error }}</p>
    <p v-if="status && !viewerOpen" class="caption" role="status">{{ status }}</p>
  </article>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import OutputImage from './OutputImage.vue'
import WorkspaceFileViewer from './WorkspaceFileViewer.vue'
import { api } from '../api'
import { saveDownload } from '../file-download'

const props = defineProps({
  chatId: { type: String, required: true },
  output: { type: Object, required: true },
  embedded: Boolean
})
const previews = computed(() => {
  const explicit = props.output.files.filter(file => file.role === 'preview' && file.previewable)
  const files = explicit.length ? explicit : props.output.files.filter(file => file.previewable)
  return files
})
const formatSize = size => size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MiB` : `${Math.max(1, Math.ceil(size / 1024))} KiB`
const openedFile = ref(''), presenting = ref(false)
const activeFile = computed(() => props.output.files.find(file => file.id === openedFile.value))
const viewerOpen = computed({ get: () => Boolean(activeFile.value), set: open => {
  if (!open) { openedFile.value = ''; presenting.value = false; error.value = ''; status.value = '' }
} })
function selectFile (id) { error.value = ''; status.value = ''; presenting.value = false; openedFile.value = id }
const loadingFile = ref('')
const error = ref('')
const status = ref('')
let controller, url
function cleanup () {
  controller?.abort()
  if (url) URL.revokeObjectURL(url)
  url = ''
  loadingFile.value = ''
  status.value = ''
}
async function download (file) {
  cleanup()
  const request = new AbortController()
  controller = request
  loadingFile.value = file.id
  error.value = ''
  try {
    const blob = await api.outputFile(props.chatId, props.output.id, props.output.revision, file.id, {
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(60000)])
    })
    if (request.signal.aborted) return
    const result = await saveDownload(blob, file.name, file.mime_type, request.signal)
    if (request.signal.aborted) {
      if (result.url) URL.revokeObjectURL(result.url)
      return
    }
    url = result.url
    if (result.cancelled === false) status.value = `Saved ${file.name} to your selected location.`
    if (result.cancelled === true) status.value = 'Save cancelled. No file was downloaded.'
  } catch (err) {
    if (!request.signal.aborted) error.value = `Could not download file: ${err.message}. Try again.`
  } finally {
    if (!request.signal.aborted) loadingFile.value = ''
  }
}
watch([() => props.chatId, () => props.output.id, () => props.output.revision], () => { cleanup(); viewerOpen.value = false })
onUnmounted(cleanup)
</script>

<style scoped>
.output-card { min-width: 0; padding: 20px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-panel); overflow-wrap: anywhere; }
.output-card__head { display: flex; align-items: flex-start; gap: 10px; }
.output-card__head > .material-icons { display: grid; place-items: center; flex: none; width: 36px; height: 36px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-app); color: var(--accent); font-size: 1.125rem; }
.output-card__head h2, .output-card__head p { margin: 0 0 6px; }
.output-card__head h2 { font-size: 1rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.5; }
.output-card__summary { white-space: pre-wrap; font-size: 0.875rem; line-height: 1.75; max-width: 75ch; color: var(--text-muted); }
.output-card__previews { display: grid; gap: 12px; margin: 12px 0; }
.output-card__files { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr)); gap: 8px; list-style: none; margin: 12px 0 0; padding: 0; }
.output-card__file { min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: stretch; gap: 5px; }
.output-card__open { min-height: 44px; }
.output-card__download { display: flex; align-items: center; gap: 8px; width: 100%; height: 100%; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-input); color: var(--text); text-align: left; font: inherit; cursor: pointer; }
.output-card__download:disabled { opacity: 0.6; cursor: wait; }
.output-card__file-details { display: flex; flex-direction: column; gap: 4px; min-width: 0; overflow-wrap: anywhere; }
.output-card__file-details strong { font-size: 0.875rem; }
.output-card__download:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.output-card__download:active:not(:disabled) { background: var(--surface-hover); }
@media (hover: hover) and (pointer: fine) { .output-card__download:hover:not(:disabled) { border-color: var(--border-strong); background: var(--surface-hover); } }
@media (max-width: 560px) { .output-card { padding: 16px; } }
.output-card--embedded { padding: 0; border: 0; border-radius: 0; }
.output-card--embedded .output-card__head > .material-icons { display: none; }
.output-card--embedded .output-card__head h2 { font-size: 1.2rem; line-height: 1.3; }
.output-card--embedded .output-card__files { grid-template-columns: minmax(0, 1fr); }
.output-card--embedded :deep(.output-image__open img) { max-height: min(17rem, 40dvh); }
.output-card__files-heading { margin: 20px 0 0; font-size: 0.9rem; font-weight: 650; }
</style>
