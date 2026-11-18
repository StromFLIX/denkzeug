<template>
  <div v-if="collapsible">
    <div :id="bodyId" ref="preview" class="markdown-preview" :class="{ 'markdown-preview--clipped': clipped }"
      :style="clipped ? { maxHeight: `${previewHeight}px` } : undefined" @focusin="revealFocused">
      <div ref="body" class="bubble__body" @click="copyCode" v-html="html" />
    </div>
    <button v-if="overflows" type="button" class="btn btn--sm btn--outline markdown-toggle"
      :aria-expanded="expanded" :aria-controls="bodyId" @click="expanded = !expanded">
      {{ expanded ? 'Show less' : 'Show more' }}
    </button>
  </div>
  <div v-else class="bubble__body" @click="copyCode" v-html="html" />
</template>

<script setup>
import { computed, nextTick, onUnmounted, ref, useId, watch } from 'vue'
import { renderMarkdown } from '../markdown'

const props = defineProps({ text: { type: String, required: true }, collapsible: Boolean })
const emit = defineEmits(['copy-code'])
const html = computed(() => renderMarkdown(props.text))
const bodyId = useId()
const body = ref(null), preview = ref(null)
const expanded = ref(false), overflows = ref(false)
const clipped = computed(() => overflows.value && !expanded.value)

// Observe the uncropped content, not the height changed by the disclosure.
const previewHeight = 320
let observer
watch(body, element => {
  observer?.disconnect()
  if (!element) return
  const measure = () => { overflows.value = element.offsetHeight > previewHeight }
  observer = new ResizeObserver(measure)
  observer.observe(element)
  measure()
}, { flush: 'post' })
onUnmounted(() => observer?.disconnect())

async function revealFocused (event) {
  const target = event.target
  if (!clipped.value || target.getBoundingClientRect().bottom + 2 <= preview.value.getBoundingClientRect().bottom) return
  // Markdown can contain links and code buttons. Never leave keyboard focus clipped.
  expanded.value = true
  await nextTick()
  target.scrollIntoView({ block: 'nearest' })
}

function copyCode (event) {
  const button = event.target.closest('button.code-block__copy')
  if (!button || !event.currentTarget.contains(button)) return
  const code = button.closest('.code-block')?.querySelector('pre code')
  if (code) emit('copy-code', code.textContent)
}
</script>

<style scoped>
.markdown-preview > .bubble__body { display: flow-root; }
.markdown-preview--clipped { overflow: clip; }
.markdown-toggle { margin-top: 8px; }
</style>

<style>
.bubble__body {
  overflow-wrap: anywhere;
  -webkit-user-select: text;
  user-select: text;
}

.bubble__body > :first-child { margin-top: 0; }
.bubble__body > :last-child { margin-bottom: 0; }
.bubble__body p { margin: 0 0 8px; line-height: 1.55; }
.bubble__body ul,
.bubble__body ol { margin: 0 0 8px; padding-left: 20px; }
.bubble__body li { margin-bottom: 3px; }

.bubble__body h1,
.bubble__body h2,
.bubble__body h3,
.bubble__body h4 {
  margin: 12px 0 6px;
  font-size: calc(var(--chat-font-size) * 1.12);
  font-weight: 600;
}

.bubble__body code { padding: 1px 5px; border-radius: var(--radius-xs); background: var(--surface-active); }
.bubble__body pre {
  margin: 8px 0;
  padding: 11px 13px;
  border-radius: var(--radius-md);
  background: var(--surface-code);
  color: var(--text);
  overflow-x: auto;
}
.bubble__body pre code { padding: 0; background: none; }
.bubble__body .code-block {
  margin: 8px 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--surface-code);
  color: var(--text);
  overflow: hidden;
}
.bubble__body .code-block__toolbar { display: flex; justify-content: flex-end; padding: 4px 8px; border-bottom: 1px solid var(--border); }
.bubble__body .code-block pre { margin: 0; border-radius: 0; background: none; }
.bubble__body a { color: inherit; text-decoration: underline; text-underline-offset: 2px; }
.msg--assistant .bubble__body a,
.execution-output .bubble__body a { color: var(--accent-hover); }
.bubble__body blockquote { margin: 8px 0; padding-left: 10px; border-left: 2px solid var(--border-strong); color: var(--text-muted); }
.bubble__body table { width: 100%; margin: 8px 0; border-collapse: collapse; font-size: 0.8125rem; }
.bubble__body th,
.bubble__body td { padding: 5px 8px; border: 1px solid var(--border); text-align: left; }
</style>
