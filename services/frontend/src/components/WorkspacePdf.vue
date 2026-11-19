<template>
  <div class="workspace-pdf">
    <div v-if="pages" class="workspace-pdf__tools" aria-label="PDF controls">
      <button type="button" class="btn btn--icon" aria-label="Previous page" :disabled="page <= 1" @click="page--"><span class="material-icons" aria-hidden="true">chevron_left</span></button>
      <label class="caption">Page <input type="number" inputmode="numeric" :value="page" min="1" :max="pages" aria-label="Page number" @change="goToPage" @keydown.enter="$event.target.blur()"> of {{ pages }}</label>
      <button type="button" class="btn btn--icon" aria-label="Next page" :disabled="page >= pages" @click="page++"><span class="material-icons" aria-hidden="true">chevron_right</span></button>
      <button type="button" class="btn btn--sm btn--outline" :aria-pressed="fit" @click="fit = !fit">{{ fit ? 'Actual size' : 'Fit width' }}</button>
    </div>
    <p v-if="error" class="caption" role="alert">Could not display this PDF. Download the original to open it.</p>
    <p v-else-if="!pages" class="caption dim" role="status">Loading PDF…</p>
    <div ref="viewport" class="workspace-pdf__page" :aria-busy="painting">
      <canvas v-show="pages && !error" ref="surface" role="img" :aria-label="`Page ${page} of ${pages}`" />
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { AnnotationMode, GlobalWorkerOptions, getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs'
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'

const props = defineProps({ bytes: { type: Uint8Array, required: true } })
const surface = ref(null), viewport = ref(null), pages = ref(0), page = ref(1), fit = ref(true)
const error = ref(false), painting = ref(false), width = ref(0)
let task, pdf, paintTask, observer, token = 0, closed = false
function goToPage (event) {
  const value = event.target.valueAsNumber
  if (Number.isFinite(value)) page.value = Math.max(1, Math.min(pages.value, Math.round(value)))
  event.target.value = page.value
}
onMounted(async () => {
  observer = new ResizeObserver(() => { width.value = viewport.value?.clientWidth || 0 })
  observer.observe(viewport.value)
  GlobalWorkerOptions.workerSrc = workerUrl
  try {
    task = getDocument({ data: props.bytes, enableXfa: false, useWasm: false, useWorkerFetch: false, disableFontFace: true, isEvalSupported: false, maxImageSize: 16_000_000 })
    pdf = await task.promise
    if (!closed) pages.value = pdf.numPages
  } catch { if (!closed) error.value = true }
})
watch([pages, page, fit, width], async () => {
  if (!pdf || !width.value) return
  const renderId = ++token
  if (paintTask) {
    paintTask.cancel()
    await paintTask.promise.catch(() => {})
  }
  if (closed || renderId !== token) return
  painting.value = true
  error.value = false
  try {
    const pdfPage = await pdf.getPage(page.value)
    if (closed || renderId !== token) return
    const base = pdfPage.getViewport({ scale: 1 })
    if (![base.width, base.height].every(value => Number.isFinite(value) && value > 0)) throw new Error('Invalid PDF page size')
    const scale = Math.min(fit.value ? Math.max(1, width.value - 12) / base.width : 1.33, 2, 4096 / Math.max(base.width, base.height))
    const box = pdfPage.getViewport({ scale })
    const pixels = Math.min(2, window.devicePixelRatio || 1, 4096 / box.width, 4096 / box.height)
    const canvas = surface.value
    canvas.width = Math.max(1, Math.ceil(box.width * pixels))
    canvas.height = Math.max(1, Math.ceil(box.height * pixels))
    canvas.style.width = `${box.width}px`
    canvas.style.height = `${box.height}px`
    paintTask = pdfPage.render({ canvas, viewport: box, transform: [pixels, 0, 0, pixels, 0, 0], annotationMode: AnnotationMode.DISABLE })
    await paintTask.promise
  } catch { if (!closed && renderId === token) error.value = true }
  finally { if (!closed && renderId === token) painting.value = false }
}, { flush: 'post' })
onUnmounted(() => { closed = true; observer?.disconnect(); paintTask?.cancel(); task?.destroy().catch(() => {}) })
</script>

<style scoped>
.workspace-pdf { min-width: 0; height: 100%; display: flex; flex-direction: column; }
.workspace-pdf__tools { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; justify-content: center; padding-bottom: 8px; }
.workspace-pdf__tools button, .workspace-pdf__tools input { min-height: 44px; }
.workspace-pdf__tools input { background: var(--surface-input); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius-sm); font: inherit; margin: 0 4px; width: 5ch; padding: 3px 4px; }
.workspace-pdf__page { overflow: auto; min-height: 0; flex: 1; background: var(--surface-rail); }
.workspace-pdf__page canvas { display: block; margin: 6px auto; background: white; box-shadow: var(--shadow-sm); max-width: none; }
</style>
