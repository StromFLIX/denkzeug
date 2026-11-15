<template>
  <section class="workflow-runs">
    <header class="runs-heading"><h2>{{ preview ? 'Recent runs' : workflow ? 'Runs' : 'All runs' }}</h2><p>Each execution records its inputs, steps and outcome.</p></header>
    <div v-if="!preview" class="runs-filters">
      <input :value="route.query.runSearch || ''" class="field" aria-label="Search runs" placeholder="Search workflows or run IDs" @input="search($event.target.value)" />
      <select class="field" aria-label="Run outcome" :value="route.query.runStatus || ''" @change="filter('runStatus', $event.target.value)">
        <option value="">All outcomes</option><option v-for="status in ['running', 'completed', 'failed', 'canceled', 'terminated', 'timed_out']" :key="status">{{ status }}</option>
      </select>
      <select class="field" aria-label="Run trigger" :value="route.query.runTrigger || ''" @change="filter('runTrigger', $event.target.value)">
        <option value="">Any trigger</option><option value="manual">Manual</option><option value="schedule">Scheduled</option><option value="webhook">Webhook</option>
      </select>
    </div>
    <p v-if="error" class="runs-error" role="alert">{{ error }} <button class="btn btn--sm" @click="load(false)">Retry</button></p>
    <div v-if="rows.length" class="runs-table">
      <div class="runs-row runs-row--head"><span>Outcome</span><span>{{ workflow ? 'Started' : 'Workflow / started' }}</span><span>Trigger</span><span>Duration</span><span /></div>
      <RouterLink v-for="row in rows" :key="row.workflow_id" class="runs-row" :to="{ path: runLocation(row.workflow, row.workflow_id), query: { ...route.query, tab: undefined } }">
        <span class="chip" :class="`chip--${RUN_TONE[row.status] || ''}`">{{ row.status.replaceAll('_', ' ') }}</span>
        <span class="run-name"><strong v-if="!workflow">{{ store.workflows.find(item => item.name === row.workflow)?.title || row.workflow }}</strong><span>{{ fullTime(row.created_at) }}</span></span>
        <span class="caption dim">{{ row.trigger }}</span><span class="caption dim">{{ duration(row) }}</span><span class="material-icons dim">chevron_right</span>
      </RouterLink>
    </div>
    <div v-else-if="!loading && !error" class="runs-empty"><h3>No runs yet</h3><p>{{ hasFilters ? 'No executions match these filters.' : 'Run a workflow to create its first execution record.' }}</p><button v-if="workflow" class="btn btn--outline" @click="$emit('run')">Configure a run</button></div>
    <div class="runs-footer"><span class="caption dim">{{ loading ? 'Loading executions…' : `${rows.length} executions shown` }}</span><RouterLink v-if="preview" class="btn btn--outline" :to="`/workflows/${encodeURIComponent(workflow)}?tab=Runs`">View all runs</RouterLink><button v-else-if="cursor" class="btn btn--outline" :disabled="loading" @click="load(true)">Load older runs</button></div>
  </section>
</template>
<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { onLiveEvent, store } from '../store'
import { RUN_TONE, fullTime } from '../format'
import { runLocation } from '../workflow-inputs'
const props = defineProps({ workflow: { type: String, default: '' }, preview: Boolean })
defineEmits(['run'])
const route = useRoute(), router = useRouter(), rows = ref([]), cursor = ref(''), loading = ref(false), error = ref('')
let version = 0, controller, timer, eventTimer
const hasFilters = computed(() => route.query.runStatus || route.query.runTrigger || route.query.runSearch)
function filter (key, value) { router.replace({ query: { ...route.query, [key]: value || undefined } }) }
function search (value) { clearTimeout(timer); timer = setTimeout(() => filter('runSearch', value), 250) }
function duration (row) {
  if (row.status === 'running' || !row.updated_at) return '—'
  const seconds = Math.max(0, Math.round(row.updated_at - row.created_at))
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}
async function load (more, preserve = false) {
  const token = ++version
  controller?.abort(); controller = new AbortController(); loading.value = true; error.value = ''
  try {
    const data = await api.runs(props.workflow, { compact: true, limit: props.preview ? 3 : more ? 25 : Math.min(100, Math.max(25, rows.value.length)),
      cursor: more ? cursor.value : '', status: props.preview ? '' : route.query.runStatus || '', trigger: props.preview ? '' : route.query.runTrigger || '', q: props.preview ? '' : route.query.runSearch || '' }, controller.signal)
    if (version !== token) return
    const tail = preserve && rows.value.length > 100 ? rows.value.slice(100).filter(item => !data.runs.some(row => row.workflow_id === item.workflow_id)) : []
    rows.value = more ? [...rows.value, ...data.runs.filter(item => !rows.value.some(row => row.workflow_id === item.workflow_id))] : [...data.runs, ...tail]
    if (!tail.length) cursor.value = data.next_cursor || ''
  } catch (failure) { if (version === token && failure.name !== 'AbortError') error.value = failure.message }
  finally { if (version === token) loading.value = false }
}
watch(() => [props.workflow, props.preview, route.query.runStatus, route.query.runTrigger, route.query.runSearch], () => { rows.value = []; cursor.value = ''; load(false) }, { immediate: true })
const off = onLiveEvent(event => {
  if (event.kind?.startsWith('run.') && (!props.workflow || !event.workflow || event.workflow === props.workflow)) {
    clearTimeout(eventTimer); eventTimer = setTimeout(() => { if (!document.hidden) load(false, true) }, 300)
  }
})
onUnmounted(() => { version++; controller?.abort(); off(); clearTimeout(timer); clearTimeout(eventTimer) })
</script>
<style scoped>
.workflow-runs { min-width: 0; }
.runs-heading h2 { margin: 0 0 8px; font-size: 1.25rem; }
.runs-heading p { margin: 0 0 24px; color: var(--text-muted); font-size: 0.875rem; }
.runs-filters { display: grid; grid-template-columns: minmax(180px, 1.8fr) 1fr 1fr; gap: 10px; margin-bottom: 26px; }
.runs-table { border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; background: var(--surface-panel); }
.runs-row { display: grid; grid-template-columns: 130px minmax(0, 1fr) 110px 90px 20px; gap: 14px; align-items: center; padding: 18px; color: var(--text); text-decoration: none; border-bottom: 1px solid var(--border); }
.runs-row:last-child { border-bottom: 0; }
.runs-row .chip { justify-self: start; }
.runs-row--head { font-size: 0.6875rem; background: var(--surface-raised); color: var(--text-muted); }
.run-name { display: grid; gap: 4px; font-size: 0.8125rem; overflow-wrap: anywhere; }
.runs-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.runs-footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 20px; }
.runs-empty { padding: 28px; border: 1px dashed var(--border-strong); border-radius: var(--radius-md); text-align: center; }
.runs-empty p { color: var(--text-muted); }
.runs-error { color: var(--danger); font-size: 0.8125rem; }
@media (hover: hover) and (pointer: fine) { a.runs-row:hover { background: var(--surface-hover); } }
@media (max-width: 1100px) { .runs-row { grid-template-columns: 110px minmax(0, 1fr) 80px 20px; } .runs-row > :nth-child(4) { display: none; } }
@media (max-width: 560px) { .runs-filters { grid-template-columns: 1fr 1fr; } .runs-filters input { grid-column: 1 / -1; } .runs-row { grid-template-columns: 100px minmax(0, 1fr) 18px; padding: 14px; gap: 8px; } .runs-row > :nth-child(3) { display: none; } .runs-row--head { display: none; } }
</style>
