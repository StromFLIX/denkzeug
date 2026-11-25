<template>
  <div v-if="text || app || appDiagnostic || !preview" class="execution-output">
    <MarkdownBody v-if="text && !app" :text="text" collapsible @copy-code="copy($event, 'Code')" />
    <p v-else-if="!app && !preview" class="caption dim">{{ result == null ? 'No output.' : 'No text output.' }}</p>
    <div class="caption dim" aria-live="polite" aria-atomic="true">{{ copyStatus }}</div>
    <p v-if="appDiagnostic" class="caption dim" role="status">The interactive view was unavailable when this result was saved. The original run data is retained.</p>
    <McpAppView v-if="app" :key="app.id" :instance="app" :workflow-id="workflowId" :fallback-text="hasSummary ? result.summary : ''" />
    <details v-if="hasRawOutput && !preview" class="execution-output__raw">
      <summary>Raw output</summary>
      <pre class="code">{{ JSON.stringify(result, null, 2) }}</pre>
    </details>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import MarkdownBody from './MarkdownBody.vue'
import McpAppView from './McpAppView.vue'
import { useCopyStatus } from '../use-copy-status'

const props = defineProps({
  result: { default: null },
  workflowId: { type: String, required: true },
  app: { type: Object, default: null },
  appDiagnostic: { type: String, default: '' },
  preview: Boolean
})
const hasSummary = computed(() => typeof props.result?.summary === 'string')
const hasRawOutput = computed(() => hasSummary.value && Object.keys(props.result).length > 1)
const text = computed(() => {
  const { result } = props
  if (result == null) return ''
  if (typeof result === 'string') return result.trim() ? result : ''
  if (hasSummary.value && result.summary.trim()) return result.summary
  return '```json\n' + JSON.stringify(result, null, 2) + '\n```'
})
const { copy, copyStatus } = useCopyStatus()
</script>

<style scoped>
.execution-output { min-width: 0; font-size: var(--chat-font-size); }
.execution-output__raw { margin-top: 12px; }
.execution-output__raw > summary { cursor: pointer; color: var(--text-muted); font-size: 0.875rem; }
.execution-output__raw > pre { margin-top: 8px; max-height: 420px; overflow: auto; }
</style>
