<template>
  <div class="api-keys">
    <header class="api-keys__head">
      <div class="grow"><h2 class="settings__title">API keys</h2><p class="settings__intro">Give each integration its own key. Limit what it can do, see how it is used, and end access at any time.</p></div>
      <button class="btn btn--primary" :disabled="loading || !catalog || busy" @click="openCreate"><span class="material-icons" aria-hidden="true">add</span>Create API key</button>
    </header>
    <p v-if="error" class="api-keys__error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status" class="api-keys__notice">{{ notice }}</p>
    <div id="api-keys" class="api-keys__inventory">
      <div class="api-keys__toolbar">
        <label class="grow">Find a key<input v-model="query" type="search" class="field" placeholder="Name or prefix" /></label>
        <label>Status<select v-model="filter" class="field"><option value="all">All keys</option><option value="Active">Active</option><option value="Expired">Expired</option><option value="Revoked">Revoked</option></select></label>
        <button class="btn btn--icon" aria-label="Refresh API keys" :disabled="loading || busy" @click="load"><span class="material-icons" aria-hidden="true">refresh</span></button>
      </div>
      <p v-if="loading" role="status" class="muted">Loading API keys…</p>
      <div v-else-if="!catalog" class="api-keys__empty"><p>Keys could not be loaded. Refresh to try again.</p></div>
      <div v-else-if="!tokens.length" class="api-keys__empty"><span class="material-icons" aria-hidden="true">key</span><h3>No keys yet</h3><p>Create a key for your first integration. Workflow links you create will appear here too.</p></div>
      <p v-else-if="!filtered.length" class="muted">No keys match these filters.</p>
      <ul v-else class="api-keys__list">
        <li v-for="token in filtered" :key="token.id" :class="{ 'api-keys__row--selected': detail?.id === token.id }">
          <div class="api-keys__identity grow">
            <button class="api-keys__name" :aria-label="`View ${token.name}`" @click="openDetail(token)">{{ token.name }}</button>
            <span class="chip" :class="tokenState(token) === 'Active' ? 'chip--success' : ''">{{ tokenState(token) }}</span>
            <p class="caption muted"><code>{{ token.prefix }}…</code> · {{ token.kind === 'webhook' ? 'Workflow webhook' : 'API key' }}</p>
            <p class="caption muted">{{ token.grants.length }} permission{{ token.grants.length === 1 ? '' : 's' }} · Expires {{ tokenDate(token.expires_at) }}</p>
            <p class="caption muted">Last used {{ tokenDate(token.last_used_at) }}</p>
          </div>
          <div class="api-keys__counts"><strong>{{ token.requests_7d.toLocaleString() }}</strong><span class="caption muted">requests / 7 days</span><span class="caption" :class="{ 'api-keys__error': token.failures_7d > 0 }">{{ token.failures_7d }} failures</span></div>
          <div class="api-keys__actions">
            <button class="btn btn--sm" @click="openDetail(token)">Activity</button>
            <button v-if="tokenState(token) === 'Active'" class="btn btn--sm" :disabled="busy" @click="confirmAction(token, 'rotate')">Rotate</button>
            <button v-if="token.revoked_at == null" class="btn btn--sm btn--danger" :disabled="busy" @click="confirmAction(token, 'revoke')">Revoke</button>
          </div>
        </li>
      </ul>
    </div>

    <section id="api-key-activity" class="api-keys__detail" aria-label="Key activity" :aria-busy="detailLoading">
      <template v-if="detail">
        <div class="api-keys__head"><div class="grow"><h3>{{ detail.name }}</h3><p class="caption muted">Created {{ tokenDate(detail.created_at) }}<template v-if="detail.revoked_at != null"> · Revoked {{ tokenDate(detail.revoked_at) }}</template></p></div><button class="btn btn--icon" aria-label="Close key activity" @click="closeDetail"><span class="material-icons" aria-hidden="true">close</span></button></div>
        <details class="api-keys__grants"><summary>Granted permissions ({{ detail.grants.length }})</summary><ul><li v-for="grant in detail.grants" :key="grant.scope"><code>{{ grant.scope }}</code><p class="caption muted">{{ grant.resources === null ? scopeKind(grant.scope) ? 'All accessible resources, including future resources' : 'Account-level permission' : grant.resources.map(resourceName).join(', ') }}</p></li></ul></details>
        <p v-if="detailError" class="api-keys__error" role="alert">{{ detailError }} <button class="btn btn--sm" :disabled="detailLoading" @click="loadDetail">Retry activity</button></p>
        <p v-if="detailLoading" role="status" class="muted">Loading activity…</p>
        <template v-else>
          <div v-if="usage" class="api-keys__usage">
            <div class="api-keys__head"><h4>Daily requests</h4><span class="caption muted">Last 30 days · UTC</span></div>
            <p class="caption"><strong>{{ usageTotals.requests.toLocaleString() }}</strong> requests, <strong>{{ usageTotals.failures.toLocaleString() }}</strong> failures</p>
            <div class="api-keys__chart" role="img" :aria-label="`Daily requests for the last 30 days: ${usageTotals.requests} requests, ${usageTotals.failures} failures. Each bar includes a daily breakdown.`">
              <div v-for="day in chartDays" :key="day.day" class="api-keys__day" :title="`${dayLabel(day.day)}: ${day.requests} requests, ${day.failures} failures`"><span class="api-keys__bar" :style="{ height: `${day.requests / chartMax * 100}%` }"><span class="api-keys__failure-bar" :style="{ height: `${day.requests ? day.failures / day.requests * 100 : 0}%` }" /></span></div>
            </div>
            <div class="api-keys__chart-labels caption muted"><span>{{ dayLabel(chartDays[0].day) }}</span><span>{{ dayLabel(chartDays.at(-1).day) }}</span></div>
            <details class="api-keys__daily"><summary class="caption">Daily breakdown</summary><ul><li v-for="day in chartDays" :key="day.day">{{ dayLabel(day.day) }}: {{ day.requests }} requests, {{ day.failures }} failures</li></ul></details>
            <p class="caption muted">Request details retained for {{ usage.retention.detail_days }} days; daily totals for {{ usage.retention.aggregate_days }} days. Failure segments are shown in red.</p>
          </div>
          <h4>Recent requests</h4>
          <p v-if="!requests.length && !detailError" class="muted">No requests recorded in the detail retention window.</p>
          <div v-if="requests.length" class="api-keys__table-wrap" tabindex="0" aria-label="Scrollable request history">
            <table class="api-keys__table"><thead><tr><th>When</th><th>Request</th><th>Result</th><th>Duration</th><th>Context</th></tr></thead><tbody>
              <tr v-for="item in requests" :key="item.id"><td>{{ tokenDate(item.created_at) }}</td><td><strong>{{ item.method }}</strong> <code>{{ item.route }}</code><p class="caption muted">{{ item.request_id }}</p></td><td><span :class="{ 'api-keys__error': item.status >= 400 }">{{ item.status }} {{ item.outcome }}</span></td><td>{{ item.duration_ms == null ? '—' : `${item.duration_ms} ms` }}</td><td><code>{{ item.required_scope || '—' }}</code><p v-if="item.resource_id" class="caption muted">{{ resourceName(item.resource_id) }}</p><p v-if="item.tool" class="caption muted">{{ item.tool }}</p><RouterLink v-if="item.run_id" :to="`/runs/${encodeURIComponent(item.run_id)}`" class="caption">View run</RouterLink></td></tr>
            </tbody></table>
          </div>
          <div v-if="total > pageSize" class="api-keys__pagination"><button class="btn btn--sm" :disabled="offset === 0 || detailLoading" @click="changePage(-1)">Previous</button><span class="caption muted">{{ offset + 1 }}–{{ Math.min(offset + pageSize, total) }} of {{ total }}</span><button class="btn btn--sm" :disabled="offset + pageSize >= total || detailLoading" @click="changePage(1)">Next</button></div>
        </template>
      </template>
      <p v-else class="caption muted">Select a key to review its permissions, daily usage and recent requests.</p>
    </section>

    <q-dialog v-model="creating" :persistent="busy">
      <div class="api-keys__dialog" role="region" aria-labelledby="create-key-title">
        <div class="api-keys__head"><h2 id="create-key-title">Create API key</h2><button class="btn btn--icon" aria-label="Close create API key" :disabled="busy" @click="creating = false"><span class="material-icons" aria-hidden="true">close</span></button></div>
        <p class="muted">Choose only what this integration needs. Your account’s access still applies.</p>
        <form class="api-keys__form" @submit.prevent="create">
          <fieldset :disabled="busy">
            <label>Name<input v-model="name" class="field" required maxlength="100" placeholder="e.g. Daily report script" autocomplete="off" autofocus /></label>
            <label>Expiry<select v-model="expiryMode" class="field" aria-label="Expiry"><option value="days">After a number of days</option><option value="date">On a specific date</option></select></label>
            <label v-if="expiryMode === 'days'">Expires in (days)<input v-model.number="days" class="field" type="number" min="1" :max="catalog.policy.max_days" required /></label>
            <label v-else>Expiry date and time<input v-model="expiryDate" class="field" type="datetime-local" required /><span class="caption muted">In your local timezone. Access ends at this exact time.</span></label>
            <p class="caption muted">Up to {{ catalog.policy.max_days }} days. Expiring keys reduce the risk of forgotten credentials.</p>
            <h3>Permissions</h3>
            <input v-model="scopeQuery" class="field" type="search" aria-label="Find a permission" placeholder="Find a permission" />
            <p v-if="!visibleScopes.length" class="caption muted">No matching permissions.</p>
            <div v-for="scope in visibleScopes" :key="scope.scope" class="api-keys__scope">
              <label class="api-keys__check"><input type="checkbox" :checked="Boolean(selected[scope.scope])" @change="toggleScope(scope, $event.target.checked)" /><span><code>{{ scope.scope }}</code><small>{{ scope.description }}</small></span></label>
              <div v-if="selected[scope.scope] && scope.resource_kind" class="api-keys__resource-choice">
                <label>Resource access for {{ scope.scope }}<select v-model="selected[scope.scope].all" :aria-label="`Resource access for ${scope.scope}`" class="field"><option :value="false">Only selected resources</option><option :value="true">All accessible resources</option></select></label>
                <p v-if="selected[scope.scope].all" class="caption api-keys__warning">Includes resources you gain access to later. Choose selected resources for a tighter boundary.</p>
                <template v-else>
                  <label>Find {{ scope.resource_kind }} resources<input v-model="selected[scope.scope].query" class="field" type="search" :aria-label="`Find resources for ${scope.scope}`" placeholder="Name or ID" /></label>
                  <div class="api-keys__resources" role="group" :aria-label="`Resources for ${scope.scope}`">
                    <label v-for="resource in matchingResources(scope)" :key="resource.id" class="api-keys__check api-keys__resource">
                      <input v-model="selected[scope.scope].resources" type="checkbox" :value="resource.id" :aria-label="`Grant ${scope.scope} on ${resource.name}`" />
                      <span>{{ resource.name }}<small class="mono">{{ resource.id }}</small></span>
                    </label>
                    <p v-if="!matchingResources(scope).length" class="caption muted">{{ resourcesFor(scope).length ? 'No matching resources.' : `No accessible ${scope.resource_kind} resources are available.` }}</p>
                  </div>
                  <p class="caption muted">{{ selected[scope.scope].resources.length }} resource{{ selected[scope.scope].resources.length === 1 ? '' : 's' }} selected. Only these resources will be granted.</p>
                </template>
              </div>
            </div>
            <p class="caption muted">{{ Object.keys(selected).length }} permission{{ Object.keys(selected).length === 1 ? '' : 's' }} selected. No other permissions will be granted.</p>
          </fieldset>
          <p v-if="createError" role="alert" class="api-keys__error">{{ createError }}</p>
          <div class="api-keys__actions"><button class="btn" type="button" :disabled="busy" @click="creating = false">Cancel</button><button class="btn btn--primary" :disabled="busy || !Object.keys(selected).length">{{ busy ? 'Creating…' : 'Create API key' }}</button></div>
        </form>
      </div>
    </q-dialog>
    <TokenSecretDialog :value="secret" @close="secret = null" />
  </div>
</template>
<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuasar } from 'quasar'
import { api } from '../../api.js'
import { dailyTokenUsage, tokenDate, tokenExpiryDate, tokenPayload, tokenState } from '../../api-keys.js'
import TokenSecretDialog from '../TokenSecretDialog.vue'
const route = useRoute(), router = useRouter(), $q = useQuasar()
const catalog = ref(null), tokens = ref([]), loading = ref(true), busy = ref(false), error = ref(''), notice = ref('')
const query = ref(''), filter = ref('all'), creating = ref(false), name = ref(''), days = ref(90), selected = reactive({}), scopeQuery = ref(''), createError = ref(''), secret = ref(null)
const expiryMode = ref('days'), expiryDate = ref('')
const detail = ref(null), requests = ref([]), usage = ref(null), offset = ref(0), total = ref(0), detailLoading = ref(false), detailError = ref('')
const pageSize = 50
let disposed = false, detailVersion = 0
const filtered = computed(() => tokens.value.filter(token => (filter.value === 'all' || tokenState(token) === filter.value) && `${token.name} ${token.prefix}`.toLowerCase().includes(query.value.trim().toLowerCase())))
const visibleScopes = computed(() => catalog.value.scopes.filter(scope => `${scope.scope} ${scope.description}`.toLowerCase().includes(scopeQuery.value.trim().toLowerCase())))
const resourcesFor = scope => catalog.value.resources.filter(item => item.kind === scope.resource_kind)
const resourceName = id => catalog.value?.resources.find(item => item.id === id)?.name || id
const scopeKind = scope => catalog.value?.scopes.find(item => item.scope === scope)?.resource_kind
const chartDays = computed(() => dailyTokenUsage(usage.value?.days || []))
const chartMax = computed(() => Math.max(1, ...chartDays.value.map(day => day.requests)))
const usageTotals = computed(() => chartDays.value.reduce((sum, day) => ({ requests: sum.requests + day.requests, failures: sum.failures + day.failures }), { requests: 0, failures: 0 }))
const dayLabel = day => new Date(day * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
const matchingResources = scope => {
  const query = selected[scope.scope].query.trim().toLowerCase()
  return resourcesFor(scope).filter(item => `${item.name} ${item.id}`.toLowerCase().includes(query))
}
function toggleScope (scope, checked) { if (checked) selected[scope.scope] = { all: false, resources: [], query: '' }; else delete selected[scope.scope] }
function openCreate () {
  name.value = ''; days.value = catalog.value.policy.default_days; expiryMode.value = 'days'; expiryDate.value = ''; scopeQuery.value = ''; createError.value = ''
  for (const scope of Object.keys(selected)) delete selected[scope]
  creating.value = true
}
async function load () {
  loading.value = true; error.value = ''
  try {
    const [definitions, inventory] = await Promise.all([api.tokenCatalog(), api.tokens()])
    if (disposed) return
    catalog.value = definitions; tokens.value = inventory.tokens
    selectFromRoute()
  } catch (failure) { if (!disposed) error.value = failure.message }
  finally { if (!disposed) loading.value = false }
}
function replaceToken (token) {
  tokens.value = [token, ...tokens.value.filter(item => item.id !== token.id)]
  if (detail.value?.id === token.id) detail.value = token
}
async function refreshInventory () {
  try { const result = await api.tokens(); if (!disposed) { tokens.value = result.tokens; if (detail.value) detail.value = tokens.value.find(item => item.id === detail.value.id) || detail.value } }
  catch (failure) { if (!disposed) error.value = `Access changed, but the list could not be refreshed: ${failure.message}` }
}
async function create () {
  if (busy.value) return
  createError.value = ''
  let payload
  try {
    payload = tokenPayload(name.value, expiryMode.value === 'days' ? days.value : catalog.value.policy.default_days, selected, catalog.value)
    if (expiryMode.value === 'date') payload.expires_at = tokenExpiryDate(expiryDate.value, catalog.value.policy.max_days)
  } catch (failure) { createError.value = failure.message; return }
  busy.value = true
  try {
    const result = await api.createToken(payload)
    if (disposed) return
    replaceToken(result.token); creating.value = false; secret.value = result
    notice.value = 'API key created. Save the secret before closing.'
  } catch (failure) { if (!disposed) createError.value = failure.message }
  finally { busy.value = false }
}
function confirmAction (token, action) {
  $q.dialog({ title: `${action === 'rotate' ? 'Rotate' : 'Revoke'} “${token.name}”?`, message: action === 'rotate' ? 'The old credential stops working immediately. Save the replacement and update your integration. Review its permissions and expiry in key activity.' : 'This credential stops working immediately. Integrations using it will lose access. This cannot be undone.', cancel: true, persistent: true, ok: { label: action === 'rotate' ? 'Rotate key' : 'Revoke key', color: action === 'rotate' ? 'primary' : 'negative' } }).onOk(() => mutate(token, action))
}
async function mutate (token, action) {
  if (busy.value || disposed) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    if (action === 'rotate') {
      const result = await api.rotateToken(token.id)
      if (disposed) return
      replaceToken({ ...token, revoked_at: Date.now() / 1000 }); replaceToken(result.token); secret.value = result
      notice.value = 'Key rotated. Update your integration with the replacement.'
    } else {
      await api.revokeToken(token.id)
      if (disposed) return
      replaceToken({ ...token, revoked_at: Date.now() / 1000 }); notice.value = 'Key revoked.'
    }
    await refreshInventory()
  } catch (failure) { if (!disposed) error.value = failure.message }
  finally { busy.value = false }
}
function selectFromRoute () {
  const token = tokens.value.find(item => item.id === route.query.token)
  if (token && detail.value?.id !== token.id) { detail.value = token; offset.value = 0; loadDetail() }
  else if (!route.query.token) { detail.value = null; requests.value = []; usage.value = null; ++detailVersion; detailLoading.value = false }
}
function openDetail (token) { router.replace({ query: { ...route.query, token: token.id }, hash: '#api-key-activity' }) }
function closeDetail () { const { token, ...query } = route.query; router.replace({ query, hash: '' }) }
async function loadDetail () {
  if (!detail.value) return
  const version = ++detailVersion, id = detail.value.id
  detailLoading.value = true; detailError.value = ''; requests.value = []; usage.value = null
  const results = await Promise.allSettled([api.tokenRequests(id, offset.value, pageSize), api.tokenUsage(id)])
  if (disposed || version !== detailVersion) return
  if (results[0].status === 'fulfilled') { requests.value = results[0].value.requests; total.value = results[0].value.total } else total.value = 0
  if (results[1].status === 'fulfilled') usage.value = results[1].value
  detailError.value = results.filter(item => item.status === 'rejected').map(item => item.reason.message).join(' ')
  detailLoading.value = false
  await nextTick()
}
function changePage (direction) { offset.value += direction * pageSize; loadDetail() }
watch([name, days, expiryMode, expiryDate, selected], () => { createError.value = '' }, { deep: true })
watch(() => route.query.token, selectFromRoute)
onMounted(load)
onUnmounted(() => { disposed = true; ++detailVersion; secret.value = null })
</script>
<style scoped>
.api-keys { max-width: 64rem; }
.api-keys__head { display: flex; align-items: start; justify-content: space-between; gap: 1rem; }
.api-keys__head .grow { min-width: 0; }
.api-keys__head h3, .api-keys__head h4 { margin: 0; }
.api-keys__head .btn { flex-shrink: 0; }
.api-keys__head .settings__intro { max-width: 42rem; }
.api-keys__inventory { margin-top: 1.5rem; border-top: 1px solid var(--border); }
.api-keys__toolbar { display: flex; align-items: end; gap: .75rem; padding: 1.25rem 0; }
.api-keys label, .api-keys__dialog label { font-size: .875rem; }
.api-keys__toolbar label, .api-keys__form label:not(.api-keys__check) { display: grid; gap: .5rem; }
.api-keys .field, .api-keys__dialog .field { width: 100%; font-size: max(16px, .875rem); min-height: 44px; }
.api-keys .btn, .api-keys__dialog .btn { min-height: 44px; touch-action: manipulation; }
.api-keys__list { list-style: none; margin: 0; padding: 0; }
.api-keys__list > li { display: flex; gap: 1.5rem; align-items: center; padding: 1.25rem .75rem; border-top: 1px solid var(--border); }
.api-keys__row--selected { background: var(--surface-hover); border-radius: var(--radius-md); }
.api-keys__identity { min-width: 12rem; overflow-wrap: anywhere; }
.api-keys__identity p { margin: .35rem 0 0; }
.api-keys__name { display: inline; background: none; border: none; color: var(--text); padding: 0; margin-right: .6rem; font: inherit; font-weight: 600; text-align: left; cursor: pointer; overflow-wrap: anywhere; }
.api-keys__name:hover { color: var(--accent); text-decoration: underline; }
.api-keys__counts { display: grid; gap: .2rem; min-width: 7rem; }
.api-keys__counts strong { font-size: 1.3rem; font-variant-numeric: tabular-nums; }
.api-keys__actions { display: flex; flex-wrap: wrap; gap: .5rem; }
.api-keys__empty { padding: 2rem 0; max-width: 32rem; }
.api-keys__empty .material-icons { color: var(--accent); font-size: 2rem; }
.api-keys__empty h3 { font-size: 1.1rem; margin: .75rem 0; }
.api-keys__empty p { color: var(--text-muted); line-height: 1.6; }
.api-keys__error { color: var(--danger); }
.api-keys__notice { background: var(--surface-hover); border-left: 3px solid var(--accent); padding: .75rem 1rem; }
.api-keys__detail { margin-top: 1.5rem; border-top: 1px solid var(--border); padding-top: 1.5rem; overflow-wrap: anywhere; }
.api-keys__detail h3 { font-size: 1.15rem; font-weight: 600; }
.api-keys__detail h4 { font-size: .95rem; font-weight: 600; }
.api-keys__grants { margin: 1rem 0 1.5rem; }
.api-keys summary { cursor: pointer; padding: .5rem 0; }
.api-keys__grants ul { padding-left: 1.25rem; }
.api-keys__grants li { padding: .25rem 0; }
.api-keys__usage { margin-bottom: 1.5rem; }
.api-keys__chart { display: flex; align-items: stretch; height: 8rem; gap: 4px; border-bottom: 1px solid var(--border); margin: 1.5rem 0 .5rem; }
.api-keys__day { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: end; border-bottom: 2px solid var(--border); }
.api-keys__bar { display: flex; flex-direction: column; justify-content: end; background: var(--accent); border-radius: 3px 3px 0 0; }
.api-keys__failure-bar { background: var(--danger); }
.api-keys__chart-labels { display: flex; justify-content: space-between; }
.api-keys__daily { margin-top: .75rem; }
.api-keys__daily ul { max-height: 12rem; overflow-y: auto; font-size: .8rem; }
.api-keys__table-wrap { overflow-x: auto; }
.api-keys__table { border-collapse: collapse; width: 100%; text-align: left; font-size: .8rem; }
.api-keys__table th { color: var(--text-muted); font-weight: 500; white-space: nowrap; }
.api-keys__table td, .api-keys__table th { padding: .75rem .5rem; border-bottom: 1px solid var(--border); vertical-align: top; }
.api-keys__table td:first-child { min-width: 8rem; }
.api-keys__table p { margin: .3rem 0 0; }
.api-keys__pagination { display: flex; align-items: center; justify-content: end; gap: 1rem; margin-top: 1rem; }
.api-keys__dialog { width: 42rem; max-width: calc(100vw - 2rem); max-height: calc(100dvh - 2rem); overflow-y: auto; padding: 1.5rem; border-radius: var(--radius-lg); background: var(--surface-panel); color: var(--text); }
.api-keys__dialog h2 { font-size: 1.3rem; font-weight: 600; margin: .4rem 0; }
.api-keys__dialog > .api-keys__head { position: sticky; top: -1.5rem; z-index: 1; background: var(--surface-panel); padding: .5rem 0; }
.api-keys__form fieldset { border: 0; padding: 0; margin: 0; min-width: 0; display: grid; gap: 1rem; }
.api-keys__form p, .api-keys__form h3 { margin: 0; }
.api-keys__form h3 { font-size: 1rem; font-weight: 600; }
.api-keys__form > .api-keys__actions { justify-content: end; margin-top: 1.5rem; }
.api-keys__scope { border-top: 1px solid var(--border); padding-top: .75rem; }
.api-keys__check { display: flex; align-items: start; gap: .75rem; cursor: pointer; min-height: 44px; }
.api-keys__check input { margin-top: .3rem; width: 1rem; height: 1rem; accent-color: var(--accent); flex-shrink: 0; }
.api-keys__check span { min-width: 0; overflow-wrap: anywhere; }
.api-keys__check small { display: block; color: var(--text-muted); line-height: 1.5; margin-top: .25rem; font-size: .8rem; }
.api-keys__resource-choice { display: grid; gap: .75rem; margin: .75rem 0 .25rem 1.75rem; }
.api-keys__resources { border: 1px solid var(--border); border-radius: var(--radius-md); padding: .25rem .75rem; max-height: 12rem; overflow-y: auto; }
.api-keys__resources > p { padding: .5rem 0; }
.api-keys__resource { padding: .5rem 0; }
.api-keys__resource + .api-keys__resource { border-top: 1px solid var(--border); }
.api-keys__resource small { font-size: .7rem; }
.api-keys__warning { color: var(--danger); }
@media (max-width: 760px) {
  .api-keys > .api-keys__head { flex-direction: column; }
  .api-keys__list > li { flex-wrap: wrap; gap: 1rem; padding-left: 0; padding-right: 0; }
  .api-keys__actions { width: 100%; }
  .api-keys__toolbar { flex-wrap: wrap; }
  .api-keys__toolbar .grow { flex-basis: 100%; }
  .api-keys__toolbar label:nth-child(2) { flex: 1; }
  .api-keys__chart { gap: 2px; }
  .api-keys__dialog { padding: 1rem; }
  .api-keys__dialog > .api-keys__head { top: -1rem; }
}
</style>
