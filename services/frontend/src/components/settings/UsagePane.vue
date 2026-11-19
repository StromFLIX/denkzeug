<template>
  <section id="usage-overview" class="usage">
    <header class="usage__head">
      <div><h2 class="settings__title">Usage</h2><p class="usage__intro">Understand token consumption across chats, agents, workflows and models.</p></div>
      <div class="usage__actions">
        <select v-model="period" class="field" aria-label="Usage period" @change="navigate()"><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option><option value="custom">Custom range</option></select>
        <button class="btn btn--outline btn--sm" :disabled="loading || exporting || !data" @click="exportRows">{{ exporting ? 'Exporting…' : 'Export CSV' }}</button>
        <button class="btn btn--icon" :disabled="loading" aria-label="Refresh usage" @click="load"><span class="material-icons" aria-hidden="true">refresh</span></button>
        <button class="btn btn--outline btn--sm usage__filter-toggle" aria-controls="usage-filters" :aria-expanded="filtersOpen" @click="filtersOpen = !filtersOpen">Filters<span v-if="filterCount"> ({{ filterCount }})</span></button>
      </div>
    </header>
    <div v-if="period === 'custom'" class="usage__dates">
      <label>From<input v-model="start" class="field" type="date" aria-label="Usage start date" @change="navigate()" /></label>
      <label>Through<input v-model="end" class="field" type="date" aria-label="Usage end date" @change="navigate()" /></label>
    </div>
    <div id="usage-filters" class="usage__filters" :class="{ 'usage__filters--open': filtersOpen }">
      <select v-if="admin" v-model="filters.user" class="field" aria-label="Account owner" @change="navigate()"><option value="">All account owners</option><option v-for="item in facets.users" :key="item.id" :value="item.id">{{ item.label }}</option></select>
      <select v-model="filters.workload" class="field" aria-label="Workload" @change="navigate()"><option value="">All workloads</option><option value="chats">Chats</option><option value="workflows">Workflows</option></select>
      <select v-model="filters.model" class="field" aria-label="Model" @change="navigate()"><option value="">All models</option><option v-for="item in facets.models" :key="item.id" :value="item.id">{{ item.label }}</option></select>
      <select v-model="filters.agent" class="field" aria-label="Saved agent" @change="navigate()"><option value="">All saved agents</option><option v-for="item in facets.agents" :key="item.id" :value="item.id">{{ item.label }}</option></select>
      <select v-if="facets.workflows.length || filters.workflow" v-model="filters.workflow" class="field" aria-label="Workflow" @change="navigate()"><option value="">All workflows</option><option v-for="item in facets.workflows" :key="item.id" :value="item.id">{{ item.label }}</option></select>
    </div>
    <div class="usage__range"><span>{{ dateLabel(start) }} – {{ dateLabel(end) }} · {{ timezone }}</span><button v-if="filterCount" class="usage__link" @click="resetFilters">Reset filters</button></div>
    <div v-if="filters.workflow" class="usage__selection"><span>Workflow: <strong>{{ filters.workflow }}</strong></span><button class="btn btn--icon" aria-label="Clear workflow filter" @click="filters.workflow = ''; navigate()"><span class="material-icons" aria-hidden="true">close</span></button></div>
    <div v-if="error" class="usage__error" role="alert">{{ error }} <button class="btn btn--sm btn--outline" @click="load">Retry</button></div>
    <p v-if="loading && !data" class="caption dim usage__loading" role="status">Loading usage…</p>
    <div v-if="data" :aria-busy="loading">
      <div class="usage__metrics">
        <div class="usage__hero">
          <div class="usage__metric"><span>Reported tokens</span><strong class="usage__total" :title="exact(data.summary.tokens?.total)">{{ compactTokens(data.summary.tokens?.total) }}</strong></div>
          <dl class="usage__tokens" aria-label="Reported tokens by type"><div v-for="item in categories" :key="item.key"><dt><i :style="{ background: item.color }" />{{ item.label }}</dt><dd :title="exact(data.summary.tokens?.[item.key])">{{ compactTokens(data.summary.tokens?.[item.key]) }}</dd></div></dl>
        </div>
        <div class="usage__metric"><span>Model responses</span><strong>{{ exact(data.summary.responses) }}</strong></div>
        <div class="usage__metric"><span>Agent executions</span><strong>{{ exact(data.summary.executions) }}</strong></div>
      </div>
      <UsageTokenChart :chart="data.chart" :days="data.daily" :summary="data.summary" :groups="chartGroups" :grouping="chartGroup" :loading="loading" @group="chooseChartGroup" />
      <section id="usage-breakdown" class="usage__breakdown" aria-label="Usage breakdown">
        <div class="usage__table-toolbar">
          <div class="usage__tabs" role="tablist" aria-label="Break down usage by"><button v-for="[key, label] in tabs" :id="`usage-tab-${key}`" :key="key" role="tab" :aria-selected="activeTab === key" aria-controls="usage-table" :tabindex="activeTab === key ? 0 : -1" @click="chooseGroup(key)" @keydown="tabKeys($event, key)">{{ label }}</button></div>
          <label class="usage__mobile-breakdown">Break down by<select v-model="group" class="field" aria-label="Usage breakdown" @change="navigate()"><option v-for="[key, label] in tabs" :key="key" :value="key">{{ label }}</option><option v-if="filters.workflow" value="runs">Workflow runs</option></select></label>
          <select v-model="sort" class="field usage__sort" aria-label="Sort usage" @change="navigate()"><option value="tokens">Most tokens</option><option value="newest">Newest first</option><option value="name">Name</option></select>
        </div>
        <p v-if="filters.workflow && executionView" class="usage__detail-title">{{ group === 'runs' ? 'Runs' : 'Executions' }} in {{ filters.workflow }}</p>
        <div id="usage-table" class="usage__table-scroll" role="tabpanel" :aria-labelledby="`usage-tab-${activeTab}`" tabindex="0">
          <table class="usage__table">
            <thead><tr><th>{{ executionView ? 'Started' : tabs.find(([key]) => key === group)?.[1] }}</th><th v-if="executionView">Agent / model</th><th>Reported tokens</th><th v-if="!executionView">Executions</th><th>Responses</th><th v-if="!executionView">Share</th><th><span class="sr-only">Open breakdown</span></th></tr></thead>
            <tbody><tr v-for="row in data.rows" :key="row.id">
              <td><template v-if="executionView"><span>{{ started(row.created_at) }}</span><small>{{ row.trigger || row.workload }}<template v-if="row.state === 'running'"> · In progress</template></small></template><template v-else><button class="usage__row-link" @click="drill(row)">{{ row.label }}</button><small v-if="group === 'models' && row.label.includes('/')">{{ row.label.split('/')[0] }}</small></template></td>
              <td v-if="executionView"><span>{{ row.agent_name }}</span><small>{{ row.model }}</small></td>
              <td class="usage__number" :title="exact(row.tokens?.total)">{{ compactTokens(row.tokens?.total) }}<div v-if="!executionView && row.tokens" class="usage__row-volume" aria-hidden="true"><span :style="{ width: `${row.tokens.total / (data.summary.tokens?.total || 1) * 100}%` }" /></div></td>
              <td v-if="!executionView" class="usage__number">{{ exact(row.executions) }}</td><td class="usage__number">{{ exact(row.responses) }}</td><td v-if="!executionView" class="usage__number usage__muted">{{ share(row.tokens?.total, data.summary.tokens?.total) }}</td>
              <td class="usage__open"><RouterLink v-if="executionView && row.href" :to="row.href" :aria-label="`Open execution ${row.id}`"><span class="material-icons" aria-hidden="true">chevron_right</span></RouterLink><button v-else-if="!executionView" class="btn btn--icon" :aria-label="`Explore ${row.label}`" @click="drill(row)"><span class="material-icons" aria-hidden="true">chevron_right</span></button></td>
            </tr><tr v-if="!data.rows.length"><td :colspan="6" class="usage__empty">No {{ group }} match this selection.</td></tr></tbody>
          </table>
        </div>
        <footer class="usage__pagination"><span>{{ data.count ? `${offset + 1}–${Math.min(offset + data.rows.length, data.count)} of ${data.count}` : '0 results' }}</span><div><button class="btn btn--sm btn--outline" :disabled="loading || !offset" @click="offset = Math.max(0, offset - 50); navigate(false)">Previous</button><button class="btn btn--sm btn--outline" :disabled="loading || offset + data.rows.length >= data.count" @click="offset += 50; navigate(false)">Next</button></div></footer>
      </section>
    </div>
  </section>
</template>

<script setup>
import { computed, nextTick, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../../api'
import { account } from '../../auth-session'
import { saveDownload } from '../../file-download'
import { BREAKDOWNS, CHART_GROUPS, TOKEN_CATEGORIES, compactTokens, dateLabel, dateRange, share } from '../../usage'
import UsageTokenChart from './UsageTokenChart.vue'

const route = useRoute(), router = useRouter()
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
const admin = computed(() => account.user?.role === 'admin')
const tabs = computed(() => BREAKDOWNS.filter(([key]) => admin.value || key !== 'users'))
const chartGroups = computed(() => CHART_GROUPS.filter(([key]) => admin.value || key !== 'users'))
const categories = TOKEN_CATEGORIES
const executionView = computed(() => ['executions', 'runs'].includes(group.value))
const activeTab = computed(() => group.value === 'runs' ? 'workflows' : group.value)
const filters = reactive({ user: '', model: '', agent: '', workload: '', workflow: '' })
const period = ref('30'), start = ref(''), end = ref(''), group = ref('models'), sort = ref('tokens'), offset = ref(0)
const chartGroup = computed(() => chartGroups.value.some(([key]) => key === route.query.chart_group) ? route.query.chart_group : 'models')
const data = ref(null), facets = ref({ models: [], agents: [], workflows: [], users: [] }), loading = ref(false), error = ref(''), exporting = ref(false), filtersOpen = ref(false)
const filterCount = computed(() => Object.values(filters).filter(Boolean).length)
const exact = value => value == null ? '—' : value.toLocaleString()
const started = value => Number.isFinite(value) ? new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: timezone }).format(new Date(value * 1000)) : '—'
function params () { return { start: start.value, end: end.value, timezone, group: group.value, sort: sort.value, ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)) } }
let controller, exportController, generation = 0
async function load () {
  const current = ++generation
  controller?.abort(); controller = new AbortController()
  loading.value = true; error.value = ''
  try {
    const response = await api.usage({ ...params(), chart_group: chartGroup.value, offset: offset.value }, controller.signal)
    if (current !== generation) return
    data.value = response; facets.value = response.facets
  } catch (failure) { if (current === generation && failure.name !== 'AbortError') error.value = failure.message || 'Could not load usage.' }
  finally { if (current === generation) loading.value = false }
}
function navigate (resetPage = true) {
  if (group.value === 'runs' && !filters.workflow) group.value = 'workflows'
  data.value = null
  if (resetPage) offset.value = 0
  if (period.value !== 'custom') { const range = dateRange(Number(period.value), timezone); start.value = range.start; end.value = range.end }
  const query = { ...params(), period: period.value, ...(chartGroup.value !== 'models' ? { chart_group: chartGroup.value } : {}), ...(offset.value ? { offset: String(offset.value) } : {}) }
  const same = Object.keys(query).length === Object.keys(route.query).length && Object.entries(query).every(([key, value]) => String(route.query[key]) === String(value))
  if (same) { data.value = null; load() } else router.replace({ path: route.path, query, hash: route.hash })
}
function chooseChartGroup (key) {
  const query = { ...route.query, chart_group: key === 'models' ? undefined : key }
  router.replace({ path: route.path, query, hash: route.hash })
}
function chooseGroup (key) { group.value = key; sort.value = key === 'executions' ? 'newest' : 'tokens'; navigate() }
function resetFilters () { for (const key of Object.keys(filters)) filters[key] = ''; navigate() }
function drill (row) {
  const key = { models: 'model', agents: 'agent', workflows: 'workflow', users: 'user' }[group.value]
  if (key) filters[key] = row.id
  group.value = key === 'workflow' ? 'runs' : 'executions'; sort.value = 'newest'; navigate()
}
async function tabKeys (event, key) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const keys = tabs.value.map(([id]) => id), index = keys.indexOf(key)
  const target = event.key === 'Home' ? keys[0] : event.key === 'End' ? keys.at(-1) : keys[(index + (event.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length]
  chooseGroup(target); await nextTick(); document.getElementById(`usage-tab-${target}`)?.focus()
}
async function exportRows () {
  exporting.value = true; error.value = ''; exportController = new AbortController()
  try {
    const blob = await api.exportUsage(params(), exportController.signal)
    const saved = await saveDownload(blob, 'denkzeug-usage.csv', 'text/csv', exportController.signal)
    if (saved?.url) setTimeout(() => URL.revokeObjectURL(saved.url), 60000)
  } catch (failure) { if (failure.name !== 'AbortError') error.value = failure.message || 'Could not export usage.' }
  finally { exporting.value = false }
}
watch(() => route.query, (query, previous) => {
  if (previous && [...new Set([...Object.keys(query), ...Object.keys(previous)])].every(key => key === 'chart_group' || String(query[key]) === String(previous[key]))) {
    // Keep the selected day, focus and table page when only the chart grouping changes.
    load()
    return
  }
  period.value = ['7', '30', '90', 'custom'].includes(query.period) ? query.period : query.start ? 'custom' : '30'
  const range = dateRange(period.value === 'custom' ? 30 : Number(period.value), timezone)
  start.value = typeof query.start === 'string' ? query.start : range.start; end.value = typeof query.end === 'string' ? query.end : range.end
  group.value = query.group === 'runs' ? (query.workflow ? 'runs' : 'workflows') : tabs.value.some(([key]) => key === query.group) ? query.group : 'models'
  sort.value = ['tokens', 'newest', 'name'].includes(query.sort) ? query.sort : 'tokens'
  offset.value = /^\d+$/.test(query.offset || '') ? Number(query.offset) : 0
  for (const key of Object.keys(filters)) filters[key] = typeof query[key] === 'string' && (key !== 'user' || admin.value) ? query[key] : ''
  exportController?.abort(); data.value = null; load()
}, { immediate: true })
onUnmounted(() => { generation++; controller?.abort(); exportController?.abort() })
</script>

<style scoped>
.usage { min-width: 0; font-variant-numeric: tabular-nums; }
.usage__head, .usage__actions, .usage__range, .usage__table-toolbar, .usage__pagination, .usage__pagination > div { display: flex; align-items: center; gap: .6rem; }
.usage__head { align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.usage__head > div:first-child { flex: 1; min-width: 14rem; }
.usage__intro { color: var(--text-muted); font-size: .75rem; line-height: 1.6; margin: .4rem 0 0; max-width: 65ch; }
.usage__actions .field { width: auto; font-size: .6875rem; min-height: 2rem; padding: .4rem .6rem; }
.usage__actions .btn { white-space: nowrap; }
.usage__filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr)); gap: .5rem; margin-top: 1.25rem; }
.usage__filters .field { width: 100%; min-width: 0; font-size: .6875rem; }
.usage__actions .usage__filter-toggle, .usage__mobile-breakdown { display: none; }
.usage__dates { display: flex; gap: .75rem; margin-top: 1rem; font-size: .7rem; }.usage__dates label { display: grid; gap: .3rem; }
.usage__range { justify-content: space-between; margin: .7rem 0 1.7rem; color: var(--text-muted); font-size: .625rem; flex-wrap: wrap; }
.usage__link, .usage__row-link { border: 0; background: transparent; color: var(--accent); font: inherit; cursor: pointer; padding: 0; text-align: left; }
.usage__row-link { color: var(--text); font-weight: 500; overflow-wrap: anywhere; }
.usage__row-link:hover, .usage__link:hover { text-decoration: underline; }
.usage__selection { display: inline-flex; align-items: center; gap: .5rem; border-radius: var(--radius-xs); background: var(--accent-soft); color: var(--accent); padding: .15rem .6rem; font-size: .75rem; margin-bottom: 1rem; overflow-wrap: anywhere; }
.usage__metrics { display: grid; grid-template-columns: minmax(0, 1.8fr) minmax(0, .8fr) minmax(0, .8fr); gap: 1rem; align-items: center; margin-bottom: 1.1rem; }
.usage__metric { display: grid; gap: .25rem; }
.usage__metric span { font-size: .6875rem; color: var(--text-muted); }
.usage__metric strong { font-size: 1.35rem; font-weight: 600; line-height: 1.2; letter-spacing: -.035em; }
.usage__metric .usage__total { font-size: clamp(1.7rem, 5vw, 2.3rem); white-space: nowrap; }
.usage__hero { display: flex; align-items: center; gap: clamp(.45rem, 2.5vw, 1.4rem); min-width: 0; }
.usage__tokens { display: grid; gap: .13rem; min-width: 0; width: 9rem; border-inline-start: 1px solid var(--border); padding-inline-start: .65rem; margin: 0; font-size: .57rem; }
.usage__tokens > div { display: flex; justify-content: space-between; gap: .2rem; }
.usage__tokens dt { display: flex; align-items: center; gap: .3rem; color: var(--text-muted); }
.usage__tokens dd { margin: 0; font-weight: 550; white-space: nowrap; }
.usage__tokens i { width: .35rem; height: .35rem; border-radius: 2px; flex: none; }
.usage__breakdown { margin-top: 1.5rem; }.usage__table-toolbar { justify-content: space-between; flex-wrap: wrap; }.usage__tabs { display: flex; gap: .3rem; flex-wrap: wrap; }.usage__tabs button { background: transparent; border: none; border-bottom: 2px solid transparent; padding: .65rem .5rem; font: inherit; font-size: .7rem; color: var(--text-muted); cursor: pointer; }.usage__tabs button[aria-selected=true] { color: var(--accent); border-color: var(--accent); font-weight: 600; }
.usage__sort { width: auto; font-size: .625rem; min-height: 2rem; padding: .3rem .5rem; }
.usage__table-scroll { position: relative; overflow-x: auto; }.usage__table { border-collapse: collapse; width: 100%; font-size: .6875rem; }.usage__table th { font-weight: 400; color: var(--text-muted); font-size: .6rem; white-space: nowrap; text-align: right; padding: .9rem .5rem; border-bottom: 1px solid var(--border); }.usage__table th:first-child, .usage__table td:first-child { text-align: left; }.usage__table td { padding: .85rem .5rem; border-bottom: 1px solid var(--border); }.usage__table small { display: block; color: var(--text-muted); font-size: .55rem; margin-top: .3rem; overflow-wrap: anywhere; }.usage__number { text-align: right; white-space: nowrap; }.usage__muted { color: var(--text-muted); }.usage__open { width: 2rem; text-align: right; }.usage__open a { color: var(--text-muted); display: inline-flex; padding: .4rem; }.usage__row-volume { width: 4rem; height: 3px; background: var(--border); margin: .35rem 0 0 auto; border-radius: 2px; overflow: hidden; }.usage__row-volume span { display: block; height: 100%; background: var(--accent); }
.usage__pagination { justify-content: space-between; font-size: .625rem; color: var(--text-muted); padding-top: 1rem; flex-wrap: wrap; }.usage__detail-title { font-size: .75rem; font-weight: 500; margin: 1rem 0 .3rem; }
.usage__empty { color: var(--text-muted); padding: 1.5rem 0; font-size: .75rem; line-height: 1.6; }.usage__error { border: 1px solid var(--danger); border-radius: var(--radius-sm); padding: .75rem; margin-bottom: 1rem; color: var(--danger); font-size: .75rem; }.usage__loading { padding: 3rem 0; }
.usage button:focus-visible, .usage a:focus-visible, .usage select:focus-visible, .usage input:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
@media (max-width: 700px) {
  .usage__head { gap: .55rem; }
  .usage__head > div:first-child { min-width: 0; }
  .usage__actions { width: 100%; flex-wrap: wrap; gap: .3rem; }
  .usage__actions .field, .usage__actions .btn, .usage__filters .field { min-height: 44px; }
  .usage__actions .usage__filter-toggle { display: flex; margin-inline-start: auto; }
  .usage__filters { display: none; margin-top: .55rem; }
  .usage__filters--open { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .usage__range { margin-bottom: .95rem; }
  .usage__metrics { grid-template-columns: 1fr 1fr; gap: .65rem; }
  .usage__hero { grid-column: 1 / -1; justify-content: space-between; }
  .usage__tokens { width: min(58%, 10rem); padding-inline-start: .5rem; }
  .usage__dates { flex-wrap: wrap; }.usage__dates label { min-width: 0; max-width: 100%; }
  .usage__tabs { display: none; }.usage__mobile-breakdown { display: flex; width: 100%; justify-content: space-between; align-items: center; font-size: .75rem; }.usage__mobile-breakdown .field { width: auto; font-size: .7rem; }
  .usage__table { min-width: 29rem; }
}
</style>
