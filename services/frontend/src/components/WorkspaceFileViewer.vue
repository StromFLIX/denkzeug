<template>
  <div class="workspace-viewer" :class="{ 'workspace-viewer--present': presenting }" @keydown="keys">
    <header class="workspace-viewer__header">
      <div><h2>{{ file.name }}</h2><p class="caption dim">{{ output.title }} · Revision {{ output.revision }}</p></div>
      <div class="workspace-viewer__actions">
        <button type="button" class="btn btn--sm btn--outline" :disabled="downloading" @click="$emit('download', file)">{{ downloading ? 'Downloading…' : 'Download original' }}</button>
        <button type="button" class="btn btn--icon" aria-label="Close file viewer" @click="$emit('close')"><span class="material-icons" aria-hidden="true">close</span></button>
      </div>
    </header>
    <div v-if="hasTools || output.files.length > 1" class="workspace-viewer__tools">
      <label v-if="output.files.length > 1 && !presenting" class="workspace-viewer__file caption">File
        <select :value="file.id" @change="$emit('select-file', $event.target.value)">
          <option v-for="asset in output.files" :key="asset.id" :value="asset.id">{{ asset.name }}{{ asset.role === 'source' ? ' (editable source)' : asset.role === 'preview' ? ' (preview)' : '' }}</option>
        </select>
      </label>
      <template v-if="viewer.kind === 'html'">
        <button v-for="item in htmlViews" :key="item.id" type="button" class="btn btn--sm btn--outline" :aria-pressed="mode === item.id" @click="mode = item.id">{{ item.name }}</button>
      </template>
      <template v-else-if="viewer.kind === 'markdown'">
        <button type="button" class="btn btn--sm btn--outline" :aria-pressed="mode === 'rendered'" @click="mode = 'rendered'">Rendered</button>
        <button type="button" class="btn btn--sm btn--outline" :aria-pressed="mode === 'source'" @click="mode = 'source'">Source</button>
      </template>
      <template v-else-if="viewer.kind === 'image'">
        <button type="button" class="btn btn--sm btn--outline" :aria-pressed="fit" @click="fit = true">Fit</button>
        <button type="button" class="btn btn--sm btn--outline" :aria-pressed="!fit" @click="fit = false">100%</button>
      </template>
      <template v-else-if="viewer.kind === 'slides' && assets.slides.length">
        <button type="button" class="btn btn--icon" aria-label="Previous slide" :disabled="slide === 0" @click="step(-1)"><span class="material-icons" aria-hidden="true">chevron_left</span></button>
        <span class="caption" aria-live="polite">{{ slide + 1 }} / {{ assets.slides.length }}</span>
        <button type="button" class="btn btn--icon" aria-label="Next slide" :disabled="slide === assets.slides.length - 1" @click="step(1)"><span class="material-icons" aria-hidden="true">chevron_right</span></button>
        <button type="button" class="btn btn--sm btn--outline" :aria-pressed="presenting" @click="emit('update:presenting', !presenting)">{{ presenting ? 'Exit presentation' : 'Present' }}</button>
      </template>
    </div>
    <nav v-if="viewer.kind === 'slides' && assets.slides.length > 1 && !presenting" class="workspace-viewer__slides" aria-label="Slide thumbnails">
      <button v-for="(asset, index) in assets.slides" :key="asset.id" type="button" class="btn btn--sm btn--outline" :aria-current="index === slide ? 'true' : undefined" :aria-label="`Open slide ${index + 1}`" @click="slide = index">
        <OutputImage thumbnail :chat-id="chatId" :output-id="output.id" :revision="output.revision" :file="asset" />{{ index + 1 }}
      </button>
    </nav>
    <p v-if="downloadError" class="workspace-viewer__notice caption" role="alert">{{ downloadError }}</p>
    <p v-if="downloadStatus || copyStatus" class="workspace-viewer__notice caption" role="status">{{ downloadStatus || copyStatus }}</p>
    <div class="workspace-viewer__stage" :class="{ 'workspace-viewer__stage--dark': viewer.kind === 'image' || viewer.kind === 'slides' }">
      <div v-if="unsupported" class="workspace-viewer__fallback">
        <span class="material-icons" aria-hidden="true">insert_drive_file</span>
        <h3>No preview available</h3>
        <p>{{ viewer.kind === 'slides' ? 'This presentation has no published slide images or PDF export.' : 'This file type does not have a Workspace preview.' }}</p>
        <button type="button" class="btn btn--sm btn--outline" :disabled="downloading" @click="$emit('download', file)">Download file</button>
      </div>
      <div v-else-if="error" class="workspace-viewer__fallback">
        <p role="alert">{{ error }}</p>
        <button type="button" class="btn btn--sm btn--outline" @click="load">Retry file preview</button>
      </div>
      <p v-else-if="loading" class="workspace-viewer__notice caption" role="status">Loading file…</p>
      <WorkspacePdf v-else-if="pdf" :bytes="pdf" />
      <iframe v-else-if="viewer.kind === 'html' && mode !== 'source'" :srcdoc="html" sandbox="" :title="`Isolated preview of ${file.name}`"
        class="workspace-viewer__frame" :class="{ 'workspace-viewer__frame--phone': mode === 'phone' }" />
      <div v-else-if="viewer.kind === 'markdown' && mode === 'rendered'" class="workspace-viewer__paper"><MarkdownBody :text="text" @copy-code="copyCode" /></div>
      <pre v-else-if="['html', 'markdown', 'text'].includes(viewer.kind)" class="workspace-viewer__source"><code>{{ text }}</code></pre>
      <div v-else-if="url" class="workspace-viewer__image" :class="{ 'workspace-viewer__image--fit': fit || viewer.kind === 'slides' }">
        <img :src="url" :alt="viewer.kind === 'slides' ? `Slide ${slide + 1}` : file.name" @error="error = 'Could not display this image.'" />
      </div>
    </div>
    <footer v-if="hint" class="workspace-viewer__hint caption dim">{{ hint }}</footer>
  </div>
</template>

<script setup>
import { computed, defineAsyncComponent, onUnmounted, ref, shallowRef, watch } from 'vue'
import { api } from '../api'
import { copyText } from '../clipboard'
import { fileViewer, presentationAssets, workspaceHtml } from '../workspace-file'
import MarkdownBody from './MarkdownBody.vue'
import OutputImage from './OutputImage.vue'
const WorkspacePdf = defineAsyncComponent(() => import('./WorkspacePdf.vue'))
const props = defineProps({
  chatId: { type: String, required: true }, output: { type: Object, required: true }, file: { type: Object, required: true },
  downloading: Boolean, presenting: Boolean, downloadError: String, downloadStatus: String
})
const emit = defineEmits(['close', 'download', 'select-file', 'update:presenting'])
const viewer = computed(() => fileViewer(props.file))
const assets = computed(() => presentationAssets(props.output.files))
const unsupported = computed(() => viewer.value.kind === 'download' || (viewer.value.kind === 'slides' && !assets.value.slides.length && !assets.value.pdf))
const htmlViews = [{ id: 'rendered', name: 'Desktop' }, { id: 'phone', name: 'Phone' }, { id: 'source', name: 'Source' }]
const mode = ref('rendered'), fit = ref(true), slide = ref(0)
const loading = ref(false), error = ref(''), text = ref(''), url = ref(''), pdf = shallowRef(null), copyStatus = ref('')
const html = computed(() => workspaceHtml(text.value))
const hasTools = computed(() => ['html', 'markdown', 'image'].includes(viewer.value.kind) || (viewer.value.kind === 'slides' && assets.value.slides.length))
const hint = computed(() => ({
  html: 'Isolated preview. Scripts, external requests and form submissions are blocked.',
  pdf: 'Read one page at a time. Download the original for text selection and search.',
  image: 'Fit shows the whole image. 100% uses its original pixel dimensions.',
  slides: assets.value.slides.length ? 'Showing the published slide images. Use arrow keys to advance.' : assets.value.pdf ? 'Showing the published PDF export.' : ''
})[viewer.value.kind])
let controller
function cleanup () {
  controller?.abort()
  if (url.value) URL.revokeObjectURL(url.value)
  url.value = ''
  pdf.value = null
}
async function load () {
  cleanup()
  error.value = ''
  loading.value = false
  if (unsupported.value) return
  const kind = viewer.value.kind
  const chosen = kind === 'slides' ? assets.value.slides[slide.value] || assets.value.pdf : props.file
  const image = kind === 'image' || (kind === 'slides' && chosen !== assets.value.pdf)
  const preview = (kind === 'slides' && image) || Boolean(viewer.value.preview)
  const typed = image ? (preview ? 'image/png' : viewer.value.mime) : null
  const request = new AbortController()
  controller = request
  loading.value = true
  try {
    if (['html', 'markdown', 'text'].includes(kind) && chosen.size > 2 * 1024 * 1024) throw new Error('This text file is too large to preview. Download the original to open it.')
    const blob = await api.outputFile(props.chatId, props.output.id, props.output.revision, chosen.id, {
      preview, signal: AbortSignal.any([request.signal, AbortSignal.timeout(30000)])
    })
    if (request.signal.aborted) return
    if (image) url.value = URL.createObjectURL(new Blob([blob], { type: typed }))
    else if (kind === 'pdf' || kind === 'slides') {
      const bytes = new Uint8Array(await blob.arrayBuffer())
      if (!request.signal.aborted) pdf.value = bytes
    } else {
      if (blob.size > 2 * 1024 * 1024) throw new Error('This text file is too large to preview. Download the original to open it.')
      const value = await blob.text()
      if (!request.signal.aborted) text.value = value
    }
  } catch (err) { if (!request.signal.aborted) error.value = `Could not open preview: ${err.message}` }
  finally { if (!request.signal.aborted) loading.value = false }
}
watch(() => props.file.id, () => { mode.value = 'rendered'; fit.value = true; slide.value = 0; copyStatus.value = '' }, { flush: 'sync' })
watch([() => props.file.id, slide], load, { immediate: true })
onUnmounted(cleanup)
function step (by) { slide.value = Math.max(0, Math.min(assets.value.slides.length - 1, slide.value + by)) }
function keys (event) {
  if (viewer.value.kind !== 'slides' || !assets.value.slides.length || event.altKey || event.metaKey || event.ctrlKey) return
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); step(event.key === 'ArrowRight' ? 1 : -1) }
  if (event.key === 'Escape' && props.presenting) { event.stopPropagation(); emit('update:presenting', false) }
}
async function copyCode (code) {
  try { await copyText(code); copyStatus.value = 'Copied code.' } catch { copyStatus.value = 'Could not copy code.' }
}
</script>

<style scoped>
.workspace-viewer { display: flex; flex-direction: column; min-width: 0; width: min(94vw, 1060px); height: min(88dvh, 960px); background: var(--surface-panel); color: var(--text); overflow: hidden; }
.workspace-viewer__header { display: flex; gap: 8px; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; padding: 10px 12px; border-bottom: 1px solid var(--border); }
.workspace-viewer__header h2 { font-size: 0.9rem; margin: 0 0 3px; overflow-wrap: anywhere; }
.workspace-viewer__header p { margin: 0; overflow-wrap: anywhere; }
.workspace-viewer__actions, .workspace-viewer__tools { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.workspace-viewer__tools { padding: 5px 12px; border-bottom: 1px solid var(--border); }
.workspace-viewer__slides { display: flex; gap: 8px; overflow-x: auto; padding: 6px 12px; background: var(--surface-rail); }
.workspace-viewer__slides button { flex: none; gap: 6px; }
.workspace-viewer__slides button[aria-current] { border-color: var(--accent); background: var(--accent-soft); }
.workspace-viewer__slides .output-image { width: 46px; height: 28px; }
.workspace-viewer button, .workspace-viewer__file select { min-height: 44px; }
.workspace-viewer__file { display: flex; align-items: center; gap: 6px; min-width: 0; }
.workspace-viewer__file select { max-width: min(60vw, 260px); background: var(--surface-input); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius-sm); font: inherit; }
.workspace-viewer__tools [aria-pressed='true'] { background: var(--accent-soft); color: var(--accent); border-color: var(--accent); }
.workspace-viewer__stage { min-width: 0; min-height: 0; flex: 1; background: var(--surface-app); overflow: auto; }
.workspace-viewer__stage--dark { background: #23263a; color: #fff; }
.workspace-viewer__fallback { margin: 24px auto; max-width: min(90%, 480px); text-align: center; padding: 14px; border-radius: var(--radius-md); background: var(--surface-panel); color: var(--text); }
.workspace-viewer__fallback h3 { font-size: 0.9rem; }
.workspace-viewer__fallback p { font-size: 0.8rem; }
.workspace-viewer__notice, .workspace-viewer__hint { padding: 5px 12px; margin: 0; }
.workspace-viewer__hint { border-top: 1px solid var(--border); }
.workspace-viewer__frame { display: block; width: calc(100% - 16px); height: calc(100% - 16px); margin: 8px auto; border: 1px solid var(--border); background: white; }
.workspace-viewer__frame--phone { width: min(calc(100% - 16px), 390px); }
.workspace-viewer__paper, .workspace-viewer__source { margin: 10px auto; width: min(calc(100% - 20px), 760px); padding: clamp(12px, 3vw, 40px); border-radius: var(--radius-sm); }
.workspace-viewer__paper { background: var(--surface-panel); border: 1px solid var(--border); }
.workspace-viewer__source { background: var(--surface-code); color: var(--text); font: var(--code-font-size)/1.6 var(--font-mono); white-space: pre-wrap; overflow-wrap: anywhere; user-select: text; }
.workspace-viewer__image { height: 100%; padding: 8px; overflow: auto; }
.workspace-viewer__image img { display: block; margin: auto; max-width: none; }
.workspace-viewer__image--fit { display: grid; grid-template: minmax(0, 1fr) / minmax(0, 1fr); place-items: center; }
.workspace-viewer__image--fit img { width: 100%; height: 100%; object-fit: contain; min-width: 0; min-height: 0; }
.workspace-viewer--present { width: 100vw; height: 100dvh; }
.workspace-viewer--present .workspace-viewer__header, .workspace-viewer--present .workspace-viewer__hint { display: none; }
@media (max-width: 650px) {
  .workspace-viewer { width: 100%; height: 100%; padding-bottom: env(safe-area-inset-bottom); }
  .workspace-viewer__header, .workspace-viewer__tools { padding-inline: 8px; }
  .workspace-viewer__hint { font-size: 0.65rem; }
}
</style>
