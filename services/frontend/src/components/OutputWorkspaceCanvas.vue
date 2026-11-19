<template>
  <div class="workspace-canvas">
    <div ref="viewport" class="workspace-canvas__viewport" :class="{ 'is-panning': gesture !== null }" :style="gridStyle"
      tabindex="0" aria-label="Output canvas. Use arrow keys to pan, plus or minus to zoom, or zero to fit all."
      @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerEnd" @pointercancel="pointerEnd" @lostpointercapture="pointerEnd" @pointerleave="pointerLeave"
      @wheel="wheel" @keydown.self="keys">
      <div class="workspace-canvas__world" :style="{ transform: `translate(${camera.x}px, ${camera.y}px) scale(${camera.zoom})` }">
        <article v-for="output in positioned" :key="output.id" class="workspace-canvas__tile"
          :style="{ left: `${positions[output.id].x}px`, top: `${positions[output.id].y}px` }">
          <button type="button" class="workspace-canvas__handle" :aria-label="`Move ${output.title} with arrow keys`"
            :data-move="output.id" @focus="reveal(output.id)" @keydown="moveByKey($event, output.id)">
            <span class="material-icons" aria-hidden="true">drag_indicator</span>{{ output.title }}
          </button>
          <button type="button" class="workspace-canvas__open" :aria-label="`Open ${output.title}`" @focus="reveal(output.id)" @click="$emit('open', output.id)">
            <OutputImage v-if="preview(output)" thumbnail :chat-id="chatId" :output-id="output.id" :revision="output.revision" :file="preview(output)" />
            <span v-else class="workspace-canvas__placeholder"><span class="material-icons" aria-hidden="true">description</span>{{ output.summary || 'Open to view files' }}</span>
            <span class="workspace-canvas__caption">Revision {{ output.revision }} · {{ output.files.length }} {{ output.files.length === 1 ? 'file' : 'files' }} <span aria-hidden="true">↗</span></span>
          </button>
        </article>
      </div>
    </div>
    <div class="workspace-canvas__tools" aria-label="Canvas controls">
      <button type="button" class="btn btn--icon" aria-label="Zoom out" :disabled="camera.zoom <= minZoom" @click="zoomBy(0.8)"><span class="material-icons" aria-hidden="true">remove</span></button>
      <span class="caption">{{ Math.round(camera.zoom * 100) }}%</span>
      <button type="button" class="btn btn--icon" aria-label="Zoom in" :disabled="camera.zoom >= maxZoom" @click="zoomBy(1.25)"><span class="material-icons" aria-hidden="true">add</span></button>
      <button type="button" class="btn btn--sm btn--outline" @click="fit">Fit all</button>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import OutputImage from './OutputImage.vue'

const props = defineProps({ chatId: { type: String, required: true }, outputs: { type: Array, required: true } })
defineEmits(['open'])
const width = 250, height = 188, stepX = width + 24, stepY = height + 24
const minZoom = 0.1, maxZoom = 2.5
const viewport = ref(null)
const camera = reactive({ x: 0, y: 0, zoom: 1 })
const positions = reactive(Object.create(null))
const gesture = ref(null)
const points = new Map()
const key = `denkzeug.workspace.positions.${props.chatId}`
try {
  const stored = JSON.parse(localStorage.getItem(key)) || {}
  for (const [id, p] of Object.entries(stored)) {
    if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) positions[id] = { x: p.x, y: p.y }
  }
} catch { /* Storage is optional. */ }
function save () {
  try { localStorage.setItem(key, JSON.stringify(positions)) } catch { /* Keep the in-memory layout. */ }
}
watch(() => props.outputs.map(output => output.id), ids => {
  const retained = new Set(ids)
  for (const id of Object.keys(positions)) if (!retained.has(id)) delete positions[id]
  const occupied = Object.values(positions)
  let slot = 0
  for (const id of [...ids].reverse()) {
    if (positions[id]) continue
    let p
    do {
      p = { x: (slot % 3) * stepX, y: Math.floor(slot / 3) * stepY }
      slot++
    } while (occupied.some(q => Math.abs(q.x - p.x) < width && Math.abs(q.y - p.y) < height))
    positions[id] = p
    occupied.push(p)
  }
  save()
}, { immediate: true, flush: 'sync' })
const positioned = computed(() => props.outputs.filter(output => positions[output.id]))
const preview = output => output.files.find(file => file.role === 'preview' && file.previewable) || output.files.find(file => file.previewable)
const gridStyle = computed(() => {
  const step = 24 * camera.zoom * Math.max(1, Math.ceil(0.45 / camera.zoom))
  return { backgroundSize: `${step}px ${step}px`, backgroundPosition: `${camera.x % step}px ${camera.y % step}px` }
})
function fit () {
  if (!viewport.value || !positioned.value.length) return
  const all = positioned.value.map(output => positions[output.id])
  const left = Math.min(...all.map(p => p.x)), top = Math.min(...all.map(p => p.y))
  const w = Math.max(...all.map(p => p.x + width)) - left, h = Math.max(...all.map(p => p.y + height)) - top
  const vw = viewport.value.clientWidth, vh = viewport.value.clientHeight
  camera.zoom = Math.max(minZoom, Math.min(1.1, (vw - 36) / w, (vh - 44) / h))
  camera.x = (vw - w * camera.zoom) / 2 - left * camera.zoom
  camera.y = (vh - h * camera.zoom) / 2 - top * camera.zoom
}
let resize
onMounted(() => {
  fit()
  let before = [viewport.value.clientWidth, viewport.value.clientHeight]
  resize = new ResizeObserver(() => {
    const next = [viewport.value.clientWidth, viewport.value.clientHeight]
    if (!next[0] || !next[1]) return
    camera.x += (next[0] - before[0]) / 2
    camera.y += (next[1] - before[1]) / 2
    before = next
  })
  resize.observe(viewport.value)
})
onUnmounted(() => { resize?.disconnect(); if (gesture.value?.moved) save() })
function zoomBy (factor, x = viewport.value.clientWidth / 2, y = viewport.value.clientHeight / 2) {
  const zoom = Math.min(maxZoom, Math.max(minZoom, camera.zoom * factor)), ratio = zoom / camera.zoom
  camera.x = x - (x - camera.x) * ratio
  camera.y = y - (y - camera.y) * ratio
  camera.zoom = zoom
}
function reveal (id) {
  const p = positions[id], vw = viewport.value.clientWidth, vh = viewport.value.clientHeight
  const x = p.x * camera.zoom + camera.x, y = p.y * camera.zoom + camera.y
  if (x + width * camera.zoom < 0 || x >= vw || y + height * camera.zoom < 0 || y >= vh) {
    camera.x = vw / 2 - (p.x + width / 2) * camera.zoom
    camera.y = vh / 2 - (p.y + height / 2) * camera.zoom
  }
}
const directions = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }
function keys (event) {
  const direction = directions[event.key]
  if (direction) { camera.x -= direction[0] * 45; camera.y -= direction[1] * 45 }
  else if (event.key === '+' || event.key === '=') zoomBy(1.25)
  else if (event.key === '-') zoomBy(0.8)
  else if (event.key === '0') fit()
  else return
  event.preventDefault()
}
function moveByKey (event, id) {
  const direction = directions[event.key]
  if (!direction) return
  event.preventDefault()
  positions[id].x += direction[0] * 16
  positions[id].y += direction[1] * 16
  reveal(id)
  save()
}
function relative (event) {
  const rect = viewport.value.getBoundingClientRect()
  return { x: event.clientX - rect.left, y: event.clientY - rect.top }
}
function wheel (event) {
  event.preventDefault()
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.value.clientHeight : 1
  if (event.ctrlKey || event.metaKey) {
    const p = relative(event)
    zoomBy(Math.exp(-event.deltaY * scale * 0.0025), p.x, p.y)
  } else {
    camera.x -= event.deltaX * scale
    camera.y -= event.deltaY * scale
  }
}
function pair () {
  const [a, b] = points.values()
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, size: Math.max(1, Math.hypot(a.x - b.x, a.y - b.y)) }
}
function pointerDown (event) {
  if (event.button !== 0 && event.button !== 1) return
  const id = event.target.closest('[data-move]')?.dataset.move
  if (!id && event.target.closest('button') && event.pointerType !== 'touch' && event.button !== 1) return
  const p = relative(event)
  points.set(event.pointerId, p)
  if (points.size === 1) gesture.value = { ...p, id: event.button === 1 ? null : id, moved: false }
  else if (points.size === 2) {
    if (gesture.value?.id && gesture.value.moved) save()
    gesture.value = { pinch: true, ...pair() }
  }
}
function pointerMove (event) {
  const g = gesture.value
  if (!g || !points.has(event.pointerId)) return
  const p = relative(event)
  points.set(event.pointerId, p)
  if (g.pinch && points.size >= 2) {
    const next = pair()
    zoomBy(next.size / g.size, g.x, g.y)
    camera.x += next.x - g.x
    camera.y += next.y - g.y
    Object.assign(g, next)
    viewport.value.setPointerCapture(event.pointerId)
  } else {
    const dx = p.x - g.x, dy = p.y - g.y
    if (!g.moved && Math.hypot(dx, dy) < 5) return
    g.moved = true
    viewport.value.setPointerCapture(event.pointerId)
    if (g.id && positions[g.id]) {
      positions[g.id].x += dx / camera.zoom
      positions[g.id].y += dy / camera.zoom
    } else { camera.x += dx; camera.y += dy }
    Object.assign(g, p)
  }
}
function pointerLeave (event) {
  if (!viewport.value.hasPointerCapture(event.pointerId)) pointerEnd(event)
}
function pointerEnd (event) {
  if (!points.delete(event.pointerId)) return
  if (gesture.value?.id && gesture.value.moved) save()
  gesture.value = points.size >= 2 ? { pinch: true, ...pair() } : points.size ? { ...points.values().next().value, moved: true } : null
}
</script>

<style scoped>
.workspace-canvas { border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; background: var(--surface-rail); }
.workspace-canvas__viewport { position: relative; height: clamp(340px, 62dvh, 780px); overflow: clip; background-image: radial-gradient(var(--border-strong) 1px, transparent 1.3px); touch-action: none; user-select: none; cursor: grab; }
.workspace-canvas__viewport.is-panning, .workspace-canvas__viewport.is-panning * { cursor: grabbing; }
.workspace-canvas__world { position: absolute; inset: 0; pointer-events: none; transform-origin: 0 0; }
.workspace-canvas__tile { position: absolute; width: 250px; height: 188px; display: flex; flex-direction: column; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-panel); box-shadow: var(--shadow-sm); pointer-events: auto; }
.workspace-canvas__tile:focus-within { z-index: 1; }
.workspace-canvas__handle, .workspace-canvas__open { width: 100%; color: var(--text); background: transparent; padding: 6px 9px; border: 0; text-align: left; font: inherit; }
.workspace-canvas__handle { display: flex; gap: 5px; align-items: center; min-height: 34px; font-size: 15px; line-height: 1.2; font-weight: 650; cursor: move; overflow-wrap: anywhere; max-height: 53px; overflow: hidden; }
.workspace-canvas__handle .material-icons { color: var(--text-muted); flex: none; font-size: 17px; }
.workspace-canvas__open { display: flex; flex-direction: column; flex: 1; min-height: 0; border-top: 1px solid var(--border); cursor: pointer; }
.workspace-canvas__open > :first-child { flex: 1; min-height: 0; width: 100%; }
.workspace-canvas__open:hover { background: var(--surface-hover); }
.workspace-canvas__placeholder { display: flex; flex-direction: column; align-items: center; justify-content: center; overflow: hidden; overflow-wrap: anywhere; gap: 5px; color: var(--text-muted); background: var(--surface-app); font-size: 13px; padding: 6px; }
.workspace-canvas__caption { display: flex; justify-content: space-between; gap: 4px; width: 100%; font-size: 12px; color: var(--text-muted); padding-top: 6px; }
.workspace-canvas__tools { display: flex; justify-content: center; align-items: center; gap: 10px; background: var(--surface-panel); border-top: 1px solid var(--border); padding: 5px; }
.workspace-canvas__tools button { min-height: 44px; }
@media (max-width: 650px) { .workspace-canvas__viewport { height: clamp(250px, 52dvh, 450px); } }
</style>
