<template>
  <div class="settings stack grow" @keydown.esc="closeCategories">
    <header class="pane-head settings__head">
      <button class="btn btn--icon" title="Back to workspace" aria-label="Back to workspace" @click="back">
        <span class="material-icons" aria-hidden="true">arrow_back</span>
      </button>
      <div class="settings__breadcrumb grow"><span>Workspace</span><span aria-hidden="true">/</span><h1>Settings</h1></div>
      <kbd class="settings__shortcut">⌘ / Ctrl ,</kbd>
      <BuildVersionPill :client-system="clientBuildInfo" :server-system="store.system" />
    </header>

    <div class="settings__searchbar">
      <div class="settings__search">
        <span class="material-icons" aria-hidden="true">search</span>
        <input ref="searchInput" v-model="query" type="search" placeholder="Search settings…" aria-label="Search settings"
          autocomplete="off" spellcheck="false" @input="categoriesOpen = false" @keydown.esc.stop.prevent="clearSearch" />
        <span v-if="searching" class="settings__result-count" role="status">{{ results.length }} found</span>
        <button v-if="query" class="btn btn--icon" aria-label="Clear settings search" @click="clearSearch">
          <span class="material-icons" aria-hidden="true">close</span>
        </button>
        <kbd v-else class="settings__search-key">/</kbd>
      </div>
      <button ref="categoryToggle" type="button" class="settings__mobile-nav field field--button"
        :aria-label="categoriesOpen ? 'Settings categories' : `Settings category: ${section.label}`"
        aria-controls="settings-categories" :aria-expanded="categoriesOpen" @click="toggleCategories">
        <span class="material-icons" aria-hidden="true">{{ section.icon }}</span>
        <span class="grow">{{ categoriesOpen ? 'Settings categories' : section.label }}</span>
        <span class="material-icons" aria-hidden="true">{{ categoriesOpen ? 'expand_less' : 'expand_more' }}</span>
      </button>
    </div>

    <div class="settings__workspace grow" :class="{ 'settings__workspace--categories': categoriesOpen }">
      <div class="settings__navigator">
        <nav id="settings-categories" ref="tree" class="settings__tree scroll-y" aria-label="Settings categories" @scroll.passive="measureTree">
          <div ref="groups" class="settings__groups">
            <section v-for="group in SETTINGS_GROUPS" :key="group" class="settings__group">
              <h2>{{ group }}</h2>
              <RouterLink v-for="item in sectionsIn(group)" :key="item.key" :to="`/settings/${item.key}`" replace
                class="settings__category" :class="{ 'settings__category--active': !searching && tab === item.key }"
                :aria-current="!searching && tab === item.key ? 'page' : undefined" @click="selectCategory">
                <span class="material-icons" aria-hidden="true">{{ item.icon }}</span>
                <span class="grow">{{ item.label }}</span>
                <span v-if="searching && resultCount(item.key)" class="settings__count">{{ resultCount(item.key) }}</span>
                <span v-else-if="item.key === 'system' && health === 'degraded'" class="dot dot--bad" aria-label="Needs attention" />
              </RouterLink>
            </section>
          </div>
        </nav>
        <div v-if="moreAbove" class="settings__edge settings__edge--top" aria-hidden="true" />
        <div class="settings__more" aria-hidden="true">
          <span class="settings__more-label" :class="{ 'settings__more-label--visible': moreBelow }">
            More categories below <span class="material-icons">expand_more</span>
          </span>
        </div>
      </div>

      <div ref="body" class="settings__body scroll-y" tabindex="-1">
        <div class="settings__inner">
          <section v-if="searching" aria-label="Settings search results">
            <div class="settings__section-head"><h2 class="settings__title">Search results</h2><span class="caption dim">All categories</span></div>
            <p v-if="!results.length" class="settings__no-results">No settings match “{{ query }}”. <button class="btn btn--sm" @click="clearSearch">Clear search</button></p>
            <RouterLink v-for="item in results" :key="`${item.section.key}-${item.id}-${item.label}`"
              :to="{ path: `/settings/${item.section.key}`, hash: `#${item.id}` }" replace class="settings__result" @click="query = ''">
              <span class="material-icons settings__result-icon" aria-hidden="true">{{ item.section.icon }}</span>
              <div class="grow">
                <span class="settings__result-path">{{ item.section.group }} / {{ item.section.label }}</span>
                <h3>{{ item.label }}</h3><p>{{ item.description }}</p>
              </div>
              <span class="settings__scope">{{ item.scope }}</span>
              <span class="material-icons settings__result-arrow" aria-hidden="true">arrow_forward</span>
            </RouterLink>
            <PackageSearch v-if="account.user?.role === 'admin' && query.trim().length >= 2" :query="query" @select="choosePackage" />
          </section>
          <!-- Search never discards the current pane's in-progress form. -->
          <div v-show="!searching" class="settings__pane">
            <div class="settings__scope-line"><span class="settings__scope">{{ section.scope }}</span><p>{{ scopeNote }}</p></div>
            <component :is="panes[tab]" ref="pane" :key="tab" />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { health, store } from '../store'
import { SETTINGS_SECTIONS, searchSettings } from '../settings-registry'
import { account } from '../auth-session.js'
import { memberSettingKeys } from '../account.js'
import PackageSearch from '../components/settings/PackageSearch.vue'
import BuildVersionPill from '../components/BuildVersionPill.vue'

const clientBuildInfo = __CLIENT_BUILD_INFO__
const route = useRoute()
const router = useRouter()
const query = ref(typeof route.query.q === 'string' ? route.query.q : '')
const searchInput = ref(null)
const body = ref(null)
const pane = ref(null)
const categoryToggle = ref(null)
const categoriesOpen = ref(false)
const tree = ref(null)
const groups = ref(null)
const moreAbove = ref(false)
const moreBelow = ref(false)
const panes = Object.fromEntries(SETTINGS_SECTIONS.map(item => [item.key, defineAsyncComponent(item.load)]))
const visibleSections = computed(() => SETTINGS_SECTIONS.filter(item => account.user?.role === 'admin' || memberSettingKeys.has(item.key)))
const SETTINGS_GROUPS = computed(() => [...new Set(visibleSections.value.map(item => item.group))])
const section = computed(() => visibleSections.value.find(item => item.key === route.params.tab) || visibleSections.value[0])
const tab = computed(() => section.value.key)
const scopeNote = computed(() => section.value.scopeNote || 'Shared across this instance, independent of this device.')
const searching = computed(() => Boolean(query.value.trim()))
const results = computed(() => searchSettings(query.value, visibleSections.value))
const sectionsIn = group => visibleSections.value.filter(item => item.group === group)
const resultCount = key => results.value.filter(item => item.section.key === key).length

function back () {
  if (/^\/(chats|workflows|runs|apps)(\/|$)/.test(window.history.state?.back || '')) router.back()
  else router.push('/chats')
}
function choosePackage (source) { query.value = ''; router.replace({ path: '/settings/packages', query: { source } }) }
function clearSearch () { query.value = ''; searchInput.value?.focus() }
function measureTree () {
  const { scrollTop, scrollHeight, clientHeight } = tree.value
  moreAbove.value = scrollTop > 1
  moreBelow.value = scrollHeight - clientHeight - scrollTop > 1
}
function revealCategory () {
  const current = tree.value?.querySelector('[aria-current="page"]')
  if (current?.getClientRects().length) current.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' })
}
function closeCategories (event) {
  if (!categoriesOpen.value || !categoryToggle.value?.getClientRects().length) return
  event?.preventDefault()
  categoriesOpen.value = false
  categoryToggle.value.focus()
}
async function toggleCategories () {
  if (categoriesOpen.value) return closeCategories()
  categoriesOpen.value = true
  await nextTick()
  const target = tree.value.querySelector('[aria-current="page"]') || tree.value.querySelector('a')
  target?.focus()
}
function selectCategory (event) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  query.value = ''
  if (!categoriesOpen.value) return
  categoriesOpen.value = false
  if (categoryToggle.value?.getClientRects().length) nextTick(() => body.value?.focus({ preventScroll: true }))
}
function shortcuts (event) {
  if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || event.target.closest('input, textarea, select, [contenteditable="true"]')) return
  event.preventDefault()
  searchInput.value?.focus()
}

let observer
let treeObserver
let treeVisible = false
let revealed = ''
function revealTarget () {
  if (!route.hash || searching.value || revealed === `${tab.value}${route.hash}`) return
  let id
  try { id = decodeURIComponent(route.hash.slice(1)) } catch { return }
  const target = document.getElementById(id)
  if (!target) { pane.value?.revealSetting?.(id); return }
  if (!body.value?.contains(target)) return
  for (let parent = target; parent && parent !== body.value; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS') parent.open = true
  }
  body.value.querySelector('.settings__target')?.classList.remove('settings__target')
  target.classList.add('settings__target')
  if (!target.matches('input, select, textarea, button, a, summary')) target.setAttribute('tabindex', '-1')
  target.focus({ preventScroll: true })
  target.scrollIntoView({ block: 'center', behavior: 'instant' })
  revealed = `${tab.value}${route.hash}`
}
watch([tab, () => route.hash, searching], async () => {
  revealed = ''
  await nextTick()
  if (body.value) body.value.scrollTop = 0
  revealCategory()
  revealTarget()
})
watch(pane, () => nextTick(revealTarget))
watch(() => route.query.q, value => { query.value = typeof value === 'string' ? value : '' })
onMounted(() => {
  document.addEventListener('keydown', shortcuts)
  // Lazy-loaded panes (and asynchronous forms) can mount after navigation.
  observer = new MutationObserver(revealTarget)
  observer.observe(body.value, { childList: true, subtree: true })
  treeObserver = new ResizeObserver(() => {
    const visible = tree.value.clientHeight > 0
    // Opening the mobile picker already focuses and scrolls to the active link.
    // Don't override a scroll the user makes before this observer fires.
    if (visible && !treeVisible && !categoriesOpen.value) revealCategory()
    treeVisible = visible
    measureTree()
  })
  treeObserver.observe(tree.value)
  treeObserver.observe(groups.value)
  revealTarget()
})
onUnmounted(() => {
  document.removeEventListener('keydown', shortcuts)
  observer?.disconnect()
  treeObserver?.disconnect()
})
</script>

<style scoped>
.settings { min-width: 0; overflow: hidden; }
.settings__head { gap: 14px; }
.settings__head button, .settings__category, .settings__mobile-nav { touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
.settings__category:focus-visible, .settings__result:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.settings__category:active { background: var(--surface-hover); }
.settings__breadcrumb { display: flex; align-items: center; gap: 12px; font-size: 0.8125rem; color: var(--text-dim); }
.settings__breadcrumb h1 { color: var(--text); font-weight: 600; }
.settings__shortcut { margin-right: 12px; }
.settings__searchbar { padding: 18px 24px; border-bottom: 1px solid var(--border); background: var(--surface-app); }
.settings__search { display: flex; align-items: center; gap: 12px; min-height: 44px; padding: 0 10px 0 14px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-input); }
.settings__search:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.settings__search > .material-icons { color: var(--text-dim); font-size: 1.25rem; }
.settings__search input { flex: 1; width: 100%; min-width: 0; background: transparent; color: var(--text); border: 0; outline: 0; font: inherit; font-size: 0.875rem; }
.settings__search input::placeholder { color: var(--text-dim); }
.settings__search input::-webkit-search-cancel-button { display: none; }
.settings__result-count { font-size: 0.6875rem; white-space: nowrap; color: var(--text-muted); }
.settings__workspace { display: grid; grid-template-columns: 240px minmax(0, 1fr); min-height: 0; }
.settings__navigator { position: relative; display: grid; grid-template-rows: minmax(0, 1fr) auto; min-height: 0; min-width: 0; border-right: 1px solid var(--border); background: var(--surface-app); }
.settings__tree { min-height: 0; background: var(--surface-app); overscroll-behavior-y: contain; scrollbar-gutter: stable; scrollbar-color: var(--text-dim) var(--surface-app); scroll-padding-block: 18px; }
.settings__tree::-webkit-scrollbar-thumb { background: var(--text-dim); }
.settings__groups { padding: 24px 12px 20px 16px; }
.settings__edge { position: absolute; inset-inline: 0 10px; height: 18px; pointer-events: none; }
.settings__edge--top { top: 0; background: linear-gradient(var(--surface-app), transparent); }
.settings__more { display: grid; place-items: center; min-height: 30px; color: var(--text-muted); font-size: 0.6875rem; }
.settings__more-label { display: flex; align-items: center; gap: 4px; visibility: hidden; }
.settings__more-label--visible { visibility: visible; }
.settings__more-label .material-icons { font-size: 1rem; }
.settings__group + .settings__group { margin-top: 28px; }
.settings__group h2 { margin: 0 10px 10px; font-size: 0.75rem; font-weight: 600; line-height: 1.5; color: var(--text); letter-spacing: 0; text-transform: none; }
.settings__category { display: flex; align-items: center; gap: 10px; min-height: 40px; padding: 9px 10px; margin: 2px 0; border-radius: var(--radius-sm); color: var(--text-muted); text-decoration: none; font-size: 0.8125rem; }
.settings__category > .material-icons { font-size: 1.0625rem; }
@media (hover: hover) and (pointer: fine) {
  .settings__category:hover { background: var(--surface-hover); color: var(--text); }
  .settings__result:hover { background: var(--surface-hover); }
}
.settings__category--active { color: var(--accent); background: var(--accent-soft); }
.settings__count { font-size: 0.625rem; color: var(--accent); }
.settings__body { min-width: 0; min-height: 0; padding: 32px 40px 48px; scroll-padding: 28px; overscroll-behavior-y: contain; }
.settings__inner { width: 100%; max-width: 880px; margin: 0 auto; }
.settings__scope-line { display: flex; align-items: flex-start; gap: 10px; color: var(--text-muted); margin-bottom: 24px; padding-bottom: 20px; border-bottom: 1px solid var(--border); }
.settings__scope-line p { margin: 3px 0 0; max-width: 66ch; font-size: 0.75rem; line-height: 1.6; }
.settings__scope { display: inline-flex; align-items: center; flex: none; min-height: 26px; padding: 3px 8px; color: var(--text-muted); background: var(--surface-panel); border-radius: var(--radius-xs); font-size: 0.6875rem; font-weight: 500; white-space: nowrap; }
.settings__section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 22px; }
.settings__result { display: flex; align-items: center; gap: 16px; padding: 18px 8px; border-bottom: 1px solid var(--border); border-radius: var(--radius-xs); text-decoration: none; color: var(--text); }
.settings__result-icon { color: var(--text-dim); font-size: 1.25rem; }
.settings__result-path { font-size: 0.625rem; color: var(--text-dim); }
.settings__result h3 { margin: 4px 0; font-weight: 600; font-size: 0.875rem; }
.settings__result p { margin: 0; color: var(--text-muted); font-size: 0.75rem; }
.settings__result-arrow { font-size: 1rem; color: var(--accent); }
.settings__no-results { color: var(--text-muted); overflow-wrap: anywhere; }
.settings__mobile-nav { display: none; }
@media (pointer: coarse) {
  .settings__search input { font-size: max(16px, 1rem); }
  .settings__category, .settings__mobile-nav { min-height: 44px; }
}
@media (max-width: 1100px) {
  .settings__workspace { grid-template-columns: 208px minmax(0, 1fr); }
  .settings__body { padding: 24px; }
}
@media (max-width: 760px) {
  .settings__workspace { grid-template-columns: minmax(0, 1fr); }
  .settings__navigator, .settings__shortcut, .settings__search-key, .settings__breadcrumb > span { display: none; }
  .settings__workspace--categories .settings__navigator { display: grid; }
  .settings__workspace--categories .settings__body { display: none; }
  .settings__navigator { border-right: 0; }
  .settings__groups { padding: 16px max(16px, env(safe-area-inset-right)) 16px max(16px, env(safe-area-inset-left)); }
  .settings__more { padding-bottom: env(safe-area-inset-bottom); }
  .settings__category { min-height: 44px; }
  .settings__searchbar { padding: 12px max(16px, env(safe-area-inset-right)) 12px max(16px, env(safe-area-inset-left)); }
  .settings__mobile-nav { display: flex; width: 100%; margin-top: 10px; font-size: 0.8125rem; }
  .settings__mobile-nav .material-icons { font-size: 1.125rem; }
  .settings__body { padding: 20px max(16px, env(safe-area-inset-right)) max(32px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); }
  .settings__head { gap: 8px; }
  .settings__scope-line { gap: 8px; margin-bottom: 20px; padding-bottom: 16px; }
  .settings__scope-line p { margin-top: 2px; }
  .settings__result-count { display: none; }
  .settings__result { gap: 10px; padding: 16px 0; }
  .settings__result .settings__scope, .settings__result-arrow { display: none; }
}
</style>
