<template>
  <q-dialog v-model="open" :maximized="mobile" transition-show="none" transition-hide="none" :transition-duration="0"
    no-refocus no-route-dismiss aria-label="Search workspace" @before-show="rememberFocus" @show="onShow" @hide="restoreFocus">
    <q-card class="command-palette" :class="{ 'command-palette--mobile': mobile }" @keydown="onKeydown">
      <div class="command-palette__search">
        <span class="material-icons" aria-hidden="true">search</span>
        <input
          ref="input" v-model="query" type="text" role="combobox" autofocus maxlength="200"
          aria-label="Search workspace" :placeholder="placeholder" inputmode="search" enterkeyhint="search"
          aria-autocomplete="list" aria-expanded="true" aria-controls="command-results"
          :aria-activedescendant="selected ? optionId(selected) : undefined"
          autocomplete="off" spellcheck="false"
        />
        <button type="button" class="btn btn--icon" aria-label="Close search" @click="open = false"><span class="material-icons" aria-hidden="true">close</span></button>
      </div>
      <div class="command-palette__scopes" role="group" aria-label="Search in">
        <button v-for="item in scopes" :key="item.key" type="button" class="command-palette__scope"
          :class="{ 'command-palette__scope--active': scope === item.key }" :aria-pressed="scope === item.key"
          @click="scope = item.key; if (!mobile) focusSearch()">{{ item.label }}</button>
      </div>
      <div v-if="scope === 'runs' && runFilters" class="command-palette__context">
        <span>{{ runFilterLabel }}</span><button type="button" class="btn btn--sm" @click="runFilters = null">Clear run filters</button>
      </div>
      <p v-if="scope === 'packages'" class="command-palette__notice">Public npm catalog. Package searches are sent to npm.</p>
      <div id="command-results" class="command-palette__results scroll-y" role="listbox" aria-label="Search results">
        <div v-for="group in groups" :key="group.key" role="group" :aria-labelledby="`command-group-${group.key}`"
          :aria-describedby="group.key === firstSecondary ? 'command-secondary' : undefined">
          <div v-if="group.key === firstSecondary" id="command-secondary" class="command-palette__secondary" aria-hidden="true">Other workspace matches</div>
          <div :id="`command-group-${group.key}`" class="command-palette__heading">
            {{ group.label }}<span>{{ group.results.length < group.count ? `${group.results.length} of ${group.count}` : group.count }}</span>
          </div>
          <div
            v-for="result in group.results" :id="optionId(result)" :key="result.key"
            role="option" :aria-selected="selected?.key === result.key" :aria-label="result.label" :aria-describedby="`${optionId(result)}-description`"
            class="command-palette__result" :class="{ 'command-palette__result--active': selected?.key === result.key }"
            @pointermove="activeKey = result.key" @mousedown.prevent @click="activate(result)"
          >
            <span class="material-icons command-palette__icon" aria-hidden="true">{{ result.icon }}</span>
            <div class="command-palette__copy">
              <div class="command-palette__title truncate">{{ result.label }}</div>
              <div :id="`${optionId(result)}-description`" class="command-palette__description truncate">{{ result.description }}</div>
            </div>
            <span v-if="selected?.key === result.key" class="material-icons command-palette__enter" aria-hidden="true">keyboard_return</span>
          </div>
        </div>
        <p v-if="pending" class="command-palette__notice" role="status">Searching {{ pendingLabel }}…</p>
        <p v-if="!results.length && !pending && !failures.length" class="command-palette__empty">{{ query.trim() ? `No results for “${query.trim()}”.` : emptyLabel }}</p>
      </div>
      <div v-if="failures.length || (scope === 'packages' && nextOffset !== null)" class="command-palette__feedback">
        <p v-for="error in failures" :key="error" class="command-palette__notice" role="alert">{{ error }}</p>
        <button v-if="failures.length" type="button" class="btn btn--sm" @click="retry++">Retry</button>
        <button v-if="scope === 'packages' && nextOffset !== null" type="button" class="btn btn--sm" :disabled="pending" @click="morePackages">More packages</button>
      </div>
      <p v-if="mobile && searchHint && !chatLoading && !chatError" class="sr-only" role="status">{{ searchHint }}</p>
      <div class="sr-only" role="status">{{ results.length }} results shown</div>
      <footer v-if="!mobile" class="command-palette__footer">
        <span><kbd>↑</kbd> <kbd>↓</kbd> to navigate <kbd>↵</kbd> to open</span>
        <span>{{ searchHint }}</span>
      </footer>
    </q-card>
  </q-dialog>
</template>

<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { store } from '../store'
import { commandSearchGroups } from '../command-search'
import { api } from '../api'
import { account } from '../auth-session.js'
import { memberSettingKeys } from '../account.js'
import { SETTINGS_SECTIONS } from '../settings-registry.js'

const open = defineModel({ type: Boolean, default: false })
const props = defineProps({ entry: { type: Object, required: true }, mobile: Boolean })
const route = useRoute()
const router = useRouter()
const input = ref(null)
const query = ref('')
const scope = ref('all')
const activeKey = ref('')
const navigating = ref(false)
const isAdmin = computed(() => account.user?.role === 'admin')
const scopes = computed(() => [
  { key: 'all', label: 'All' }, { key: 'chats', label: 'Chats' },
  { key: 'workflows', label: 'Workflows' }, { key: 'runs', label: 'Runs' }, { key: 'settings', label: 'Settings' },
  ...(isAdmin.value ? [{ key: 'packages', label: 'Pi packages' }] : [])
])
const settingSections = computed(() => SETTINGS_SECTIONS.filter(section => isAdmin.value || memberSettingKeys.has(section.key)))
const placeholder = computed(() => scope.value === 'all' ? 'Search workspace…' : `Search ${scopes.value.find(item => item.key === scope.value)?.label.toLowerCase()}…`)
const emptyLabel = computed(() => scope.value === 'packages' ? 'Enter a package name or keyword.' : 'No items to show yet.')
const runFilters = ref(null)
const runFilterLabel = computed(() => [runFilters.value?.workflow && (store.workflows.find(item => item.name === runFilters.value.workflow)?.title || runFilters.value.workflow), runFilters.value?.status, runFilters.value?.trigger].filter(Boolean).join(' · '))
const text = computed(() => query.value.trim())
const chatQuery = computed(() => open.value && ['all', 'chats'].includes(scope.value) ? text.value : '')
const runQuery = computed(() => open.value && (scope.value === 'runs' || (scope.value === 'all' && text.value)) ? JSON.stringify([text.value, scope.value === 'runs' ? runFilters.value : null]) : '')
const packageQuery = computed(() => open.value && scope.value === 'packages' && isAdmin.value ? text.value : '')
const remoteChats = ref(null), remoteRuns = ref(null), packages = ref([]), nextOffset = ref(null)
const chatLoading = ref(false), runLoading = ref(false), packageLoading = ref(false)
const chatError = ref(''), runError = ref(''), packageError = ref(''), semanticState = ref('')
const retry = ref(0)
const pending = computed(() => chatLoading.value || runLoading.value || packageLoading.value)
const pendingLabel = computed(() => scope.value === 'packages' ? 'npm' : chatLoading.value && !runLoading.value ? 'full conversations' : runLoading.value && !chatLoading.value ? 'run history' : 'workspace')
const failures = computed(() => [chatError.value, runError.value, packageError.value].filter(Boolean))
const searchHint = computed(() => {
  if (!chatQuery.value) return ''
  if (semanticState.value === 'ready') return 'Keyword + semantic chat search'
  if (semanticState.value === 'indexing') return 'Hybrid search · indexing history'
  if (semanticState.value === 'loading') return 'Keyword search · loading semantic model'
  return remoteChats.value === null ? 'Titles & latest previews' : 'Keyword search'
})
const groups = computed(() => commandSearchGroups(store.chats, query.value, scope.value, remoteChats.value, {
  workflows: store.workflows, drafts: isAdmin.value ? store.drafts : [],
  runs: remoteRuns.value ?? (runQuery.value ? [] : store.runs), packages: packages.value, settingSections: settingSections.value
}))
const firstSecondary = computed(() => scope.value === 'all' && text.value ? groups.value.find(group => group.key !== 'chats')?.key : undefined)
const results = computed(() => groups.value.flatMap(group => group.results))
const selected = computed(() => results.value.find(result => result.key === activeKey.value) || results.value[0])
const optionId = result => `command-option-${result.key}`

// Independent jobs let All → Chats reuse the same in-flight or completed chat search.
// A query/scope change aborts only the providers it invalidates.
function searchJob (source, loading, error, reset, fetchResults, apply, failure) {
  watch([source, retry], ([key], _, cleanup) => {
    reset()
    error.value = ''
    loading.value = Boolean(key)
    if (!key) return
    const controller = new AbortController()
    const timer = setTimeout(async () => {
      try {
        const data = await fetchResults(key, AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]))
        if (!controller.signal.aborted) apply(data)
      } catch (reason) {
        if (!controller.signal.aborted) error.value = typeof failure === 'function' ? failure(reason) : failure
      } finally {
        if (!controller.signal.aborted) loading.value = false
      }
    }, 180)
    cleanup(() => { clearTimeout(timer); controller.abort() })
  })
}
searchJob(chatQuery, chatLoading, chatError, () => { remoteChats.value = null; semanticState.value = '' },
  (query, signal) => api.searchChats(query, signal), data => {
    if (!Array.isArray(data.results) || !data.semantic?.state) throw new Error('Invalid chat search response')
    remoteChats.value = data.results
    semanticState.value = data.semantic.state
  }, 'Full-history search unavailable. Showing title and latest-message matches.')
searchJob(runQuery, runLoading, runError, () => { remoteRuns.value = null },
  (key, signal) => {
    const [query, filters] = JSON.parse(key)
    // The run API searches IDs and workflow names. A human-facing workflow title
    // is mapped to its name when it is an exact match.
    const title = store.workflows.find(item => item.title?.trim().toLowerCase() === query.toLowerCase())
    return api.runs(filters?.workflow || '', { compact: true, limit: 30, q: (title?.name || query).slice(0, 120),
      status: filters?.status || '', trigger: filters?.trigger || '' }, signal)
  }, data => {
    if (!Array.isArray(data.runs)) throw new Error('Invalid run search response')
    remoteRuns.value = data.runs
  }, 'Run history is unavailable.')

let packagePage
function applyPackages (data, append = false) {
  if (!Array.isArray(data.packages)) throw new Error('Invalid package search response')
  packages.value = append ? [...new Map([...packages.value, ...data.packages].map(item => [item.name, item])).values()] : data.packages
  nextOffset.value = Number.isInteger(data.next_offset) && data.next_offset <= 1000 && packages.value.length < 100 ? data.next_offset : null
}
searchJob(packageQuery, packageLoading, packageError, () => {
  packagePage?.abort(); packages.value = []; nextOffset.value = null
}, (query, signal) => api.searchPackages(query.slice(0, 120), 0, signal), applyPackages,
error => error.message || 'Package search is unavailable.')
async function morePackages () {
  const controller = new AbortController()
  packagePage = controller
  packageLoading.value = true; packageError.value = ''
  try {
    const data = await api.searchPackages(packageQuery.value.slice(0, 120), nextOffset.value,
      AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]))
    if (!controller.signal.aborted) applyPackages(data, true)
  } catch (error) {
    if (!controller.signal.aborted) packageError.value = error.message
  } finally {
    if (!controller.signal.aborted) packageLoading.value = false
  }
}
onUnmounted(() => packagePage?.abort())

let previousFocus = null
function rememberFocus () { previousFocus = document.activeElement }
function restoreFocus () {
  if (!navigating.value && previousFocus?.isConnected) previousFocus.focus({ preventScroll: true })
  previousFocus = null
}
function focusSearch () { input.value?.focus() }
function revealSelected () {
  if (open.value && selected.value) document.getElementById(optionId(selected.value))?.scrollIntoView({ block: 'nearest' })
}
function onShow () {
  focusSearch()
  // Early keyboard navigation can happen while the dialog is still scaling in.
  revealSelected()
}
watch(open, value => {
  if (!value) return
  query.value = props.entry.query || ''
  scope.value = scopes.value.some(item => item.key === props.entry.scope) ? props.entry.scope : 'all'
  runFilters.value = props.entry.runFilters || null
  activeKey.value = ''
  navigating.value = false
}, { flush: 'sync' })
watch([query, scope], () => { activeKey.value = ''; nextTick(() => { document.getElementById('command-results')?.scrollTo(0, 0) }) })
watch(() => route.fullPath, () => {
  if (!open.value) return
  navigating.value = true
  open.value = false
})

function onKeydown (event) {
  if (event.isComposing || event.keyCode === 229 || event.altKey || event.ctrlKey || event.metaKey) return
  // Keep native button/Tab behavior; the combobox owns result navigation.
  if (event.target !== input.value) return
  if (event.key === 'Enter') {
    event.preventDefault()
    if (selected.value) activate(selected.value)
  } else if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
    event.preventDefault()
    if (!results.value.length) return
    const index = results.value.findIndex(result => result.key === selected.value?.key)
    const next = (index + (event.key === 'ArrowDown' ? 1 : -1) + results.value.length) % results.value.length
    activeKey.value = results.value[next].key
    nextTick(revealSelected)
  }
}

async function activate (result) {
  // Don't restore focus to the old page while a setting deep-link takes focus.
  const target = router.resolve(result.to)
  navigating.value = target.fullPath !== route.fullPath || target.hash.startsWith('#message-')
  open.value = false
  // Settings categories are one workspace visit: keep its original Back target.
  if (route.name === 'settings' && target.name === 'settings') await router.replace(result.to)
  else await router.push(result.to)
}
</script>

<style scoped>
.command-palette {
  display: flex;
  flex-direction: column;
  width: 720px;
  max-width: calc(100vw - 64px);
  height: min(620px, calc(100dvh - 96px));
  overflow: hidden;
}
.command-palette__search { display: flex; align-items: center; gap: 16px; padding: 20px; border-bottom: 1px solid var(--border); }
.command-palette__search:focus-within { border-bottom-color: var(--accent); }
.command-palette__search > .material-icons { color: var(--accent); font-size: 1.5rem; }
.command-palette__search input { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--text); font: inherit; font-size: 1.0625rem; }
.command-palette__search input::placeholder { color: var(--text-dim); }
.command-palette__scopes { display: flex; flex: none; flex-wrap: wrap; gap: 4px; padding: 10px 20px 6px; }
.command-palette__context, .command-palette__feedback { display: flex; flex-wrap: wrap; flex: none; align-items: center; gap: 4px 8px; padding: 4px 16px; color: var(--text-muted); font-size: .75rem; }
.command-palette__feedback { border-top: 1px solid var(--border); }
.command-palette__feedback p { margin: 0; padding: 4px 0; }
.command-palette__scope { padding: 5px 12px; border: 0; border-radius: var(--radius-pill); color: var(--text-muted); background: transparent; font: inherit; font-size: 0.75rem; cursor: pointer; }
.command-palette__scope:hover { background: var(--surface-hover); }
.command-palette__scope--active { color: var(--accent); background: var(--accent-soft); }
.command-palette__results { flex: 1; min-height: 0; padding: 0 10px 12px; }
.command-palette__heading { display: flex; gap: 8px; padding: 16px 12px 8px; color: var(--text-dim); font-size: 0.6875rem; font-weight: 600; }
.command-palette__heading span { font-weight: 400; }
.command-palette__secondary { margin: 14px 12px 0; padding-top: 12px; border-top: 1px solid var(--border); color: var(--text-muted); font-size: 0.75rem; font-weight: 600; }
.command-palette__secondary + .command-palette__heading { padding-top: 8px; }
.command-palette__result { display: flex; align-items: center; gap: 14px; padding: 12px; border-radius: var(--radius-sm); cursor: pointer; }
.command-palette__result--active { background: var(--accent-soft); }
.command-palette__icon { flex: none; color: var(--text-muted); font-size: 1.125rem; }
.command-palette__result--active .command-palette__icon { color: var(--accent); }
.command-palette__copy { flex: 1; min-width: 0; }
.command-palette__title { font-size: 0.875rem; font-weight: 550; }
.command-palette__description { margin-top: 3px; font-size: 0.75rem; color: var(--text-dim); }
.command-palette__enter { color: var(--accent); font-size: 1rem; }
.command-palette__notice { padding: 8px 12px; color: var(--text-dim); font-size: 0.75rem; }
.command-palette__empty { padding: 48px 20px; text-align: center; color: var(--text-muted); overflow-wrap: anywhere; }
.command-palette__footer { display: flex; flex: none; justify-content: space-between; gap: 12px; padding: 12px 20px; border-top: 1px solid var(--border); color: var(--text-dim); font-size: 0.6875rem; }
.command-palette__footer kbd { font-size: inherit; }
.command-palette--mobile { width: 100%; max-width: 100%; max-height: var(--app-height); container: search-palette / size; }
.command-palette--mobile .command-palette__search { flex: none; gap: 8px; padding: 6px max(12px, env(safe-area-inset-right)) 6px max(12px, env(safe-area-inset-left)); }
.command-palette--mobile .command-palette__scopes { padding: 4px 8px; }
.command-palette--mobile .command-palette__scope { padding-inline: 8px; }
.command-palette--mobile .command-palette__result { min-height: 48px; gap: 8px; }
.command-palette--mobile .command-palette__description { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; white-space: normal; overflow-wrap: anywhere; }
.command-palette--mobile .command-palette__enter { display: none; }
@media (pointer: coarse) {
  .command-palette__search input { font-size: max(16px, 1rem); }
  .command-palette__scope { min-height: 44px; }
}
@container search-palette (max-height: 400px) {
  .command-palette__scopes { flex-wrap: nowrap; overflow-x: auto; }
  .command-palette__scope { flex: none; }
}
</style>
