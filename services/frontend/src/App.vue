<template>
  <div class="app-frame" :class="{ 'app-frame--desktop-overlay': desktopOverlay }">
    <div v-if="desktopOverlay" class="desktop-titlebar" aria-hidden="true" />
    <EnvironmentBanner />
    <AccountGate v-if="gate" :fragment="authFragment" @done="authFragment = null" />
    <ReauthDialog />
    <div v-if="!gate && ['reconnecting', 'failed'].includes(store.liveStatus)" class="connection-notice" role="status" aria-label="Live connection">
      {{ store.liveStatus === 'failed' ? 'Live updates are disconnected.' : 'Connection interrupted. Reconnecting automatically…' }}
      <button v-if="store.liveStatus === 'failed'" class="btn" @click="actions.connect()">Reconnect</button>
    </div>
  <div v-if="!gate" :key="account.user.id" class="shell" data-skin-part="shell" :class="{ 'shell--detail': hasSelection, 'shell--full': fullPage, 'shell--collapsed': sideCollapsed }">
    <NavRail class="shell__rail" :mobile="mobile" :sidebar-collapsed="sideCollapsed" @open-sidebar="openSidebar" />
    <template v-if="!fullPage">
      <aside v-show="!sideCollapsed" data-skin-part="sidebar" id="shell-sidebar" class="shell__side" :style="{ width: `${sideWidth}px` }">
        <SidePanel :mobile="mobile" :is-chat-active="isChatActive">
          <template v-if="showAgentRail" #agents><AgentRail /></template>
        </SidePanel>
        <div
          class="shell__grip"
          :class="{ 'shell__grip--active': dragging }"
          role="separator" aria-label="Resize sidebar" aria-orientation="vertical"
          :aria-valuenow="sideWidth" :aria-valuemin="260" :aria-valuemax="560" tabindex="0"
          @pointerdown="startDrag" @keydown="resizeKeys"
          @dblclick="resetWidth"
        />
      </aside>
    </template>

    <main class="shell__main" data-skin-part="main">
      <RouterView />
    </main>

    <CommandPalette v-model="searchOpen" :entry="searchEntry" :mobile="mobile" />
    <McpLoginDialog />

  </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import EnvironmentBanner from './components/EnvironmentBanner.vue'
import NavRail from './components/NavRail.vue'
import AccountGate from './components/AccountGate.vue'
import ReauthDialog from './components/ReauthDialog.vue'
import { account, initialAuthFragment } from './auth-session.js'
import { accountApi, canOpenRoute } from './account.js'
import AgentRail from './components/AgentRail.vue'
import SidePanel from './components/SidePanel.vue'
import CommandPalette from './components/CommandPalette.vue'
import McpLoginDialog from './components/McpLoginDialog.vue'
import { actions, store } from './store'
import { currentTokens, preferences } from './preferences'
import { syncSystemBars } from './system-bars'
import { useChatActivity } from './chat-activity'
import { provideWorkspaceSearch } from './workspace-search.js'

const desktopOverlay = (globalThis.denkzeugDesktop || globalThis.nautionetteDesktop)?.windowControlsOverlay === true
const DEFAULT_WIDTH = 320
const route = useRoute()
const router = useRouter()
const selectedChatId = computed(() => route.name === 'chats' ? route.params.id || '' : '')
const isChatActive = useChatActivity(selectedChatId, preferences, () => route.fullPath)
const searchOpen = ref(false)
const searchEntry = ref({ scope: 'all' })
const sideWidth = computed({ get: () => preferences.sideWidth, set: value => { preferences.sideWidth = value } })
const dragging = ref(false)
const authFragment = ref(initialAuthFragment)
const gate = computed(() => !account.ready || !account.user || account.user.must_change_password || Boolean(authFragment.value))

const welcomeOpen = computed(() => route.name === 'chats' && !route.params.id && route.query.welcome === '1')
const hasSelection = computed(() => Boolean(route.params.id || route.params.name) || welcomeOpen.value || (route.name === 'workflows' && route.query.view === 'runs'))
// Settings replaces the list, but keeps the desktop navigation in reach.
const fullPage = computed(() => route.name === 'settings')
const mobileMedia = window.matchMedia('(max-width: 900px)')
const mobile = ref(mobileMedia.matches)
const updateMobile = event => { mobile.value = event.matches }
// Keep the desktop preference without hiding the list on narrow screens.
const sideCollapsed = computed(() => !mobile.value && preferences.sideCollapsed)
const showAgentRail = computed(() => account.user?.role === 'admin' && route.name === 'chats')

function openSidebar () {
  if (!mobile.value) preferences.sideCollapsed = false
}

watch([() => preferences.theme, currentTokens, hasSelection, fullPage, mobile],
  ([theme, tokens, detail, full, narrow]) => syncSystemBars(theme, tokens, narrow && !detail && !full),
  { immediate: true, flush: 'post' })

let stopDrag = () => {}
watch([sideCollapsed, fullPage, mobile], ([collapsed, full, narrow]) => {
  if (collapsed || full || narrow) stopDrag()
})
function startDrag (event) {
  if (event.button !== 0) return
  event.preventDefault()
  stopDrag()
  dragging.value = true
  const origin = event.clientX
  const start = sideWidth.value
  const move = (moveEvent) => {
    sideWidth.value = Math.round(Math.min(560, Math.max(260, start + moveEvent.clientX - origin)))
  }
  stopDrag = () => {
    dragging.value = false
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', stopDrag)
    window.removeEventListener('pointercancel', stopDrag)
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', stopDrag)
  window.addEventListener('pointercancel', stopDrag)
}

function resetWidth () {
  sideWidth.value = DEFAULT_WIDTH
}

function resizeKeys (event) {
  if (!['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) return
  event.preventDefault()
  if (event.key === 'Home') resetWidth()
  else sideWidth.value = Math.min(560, Math.max(260, sideWidth.value + (event.key === 'ArrowRight' ? 10 : -10)))
}

function openSearch (scope = 'all', options = {}) {
  if (gate.value) return
  searchEntry.value = { ...options, scope }
  searchOpen.value = true
}
provideWorkspaceSearch(openSearch)
watch([gate, () => account.epoch], () => { searchOpen.value = false })

function shortcuts (event) {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return
  if (!gate.value && !searchOpen.value && route.name === 'settings' && event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey &&
      !event.target.closest?.('input, textarea, select, [contenteditable], [role="dialog"]')) {
    event.preventDefault()
    if (!event.repeat) openSearch('settings')
  } else if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k' && !gate.value) {
    event.preventDefault()
    if (event.repeat) return
    if (searchOpen.value) searchOpen.value = false
    else openSearch()
  } else if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key === ',') {
    event.preventDefault()
    router.push('/settings/account')
  }
}

watch([gate, () => account.epoch], ([blocked]) => {
  actions.resetAccount()
  if (!blocked) { actions.connect(); actions.refreshAll().catch(() => {}) }
})
watch([() => route.fullPath, () => account.user?.role, gate], () => {
  if (!gate.value && !canOpenRoute(route)) router.replace('/chats')
}, { immediate: true })

onMounted(() => {
  mobileMedia.addEventListener('change', updateMobile)
  accountApi.initialize()
  window.addEventListener('keydown', shortcuts)
})
onUnmounted(() => {
  mobileMedia.removeEventListener('change', updateMobile)
  actions.disconnect()
  stopDrag()
  window.removeEventListener('keydown', shortcuts)
})
</script>

<style scoped>
.connection-notice { flex: none; padding: 6px 16px; background: var(--surface-raised); color: var(--text-muted); font-size: 0.875rem; text-align: center; }
.app-frame {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* This strip is outside scrollable/interactive content, including modal
   backdrops. Electron draws the native caption buttons above its right edge.
   Physical CSS pixels intentionally do not follow the app's font-size setting. */
.app-frame--desktop-overlay { padding-top: 32px; }
.desktop-titlebar {
  position: fixed;
  inset: 0 0 auto;
  height: 32px;
  z-index: 10000;
  background: #202020;
  -webkit-app-region: drag;
  app-region: drag;
  user-select: none;
}
:global(.desktop-fullscreen) .app-frame--desktop-overlay { padding-top: 0; }
:global(.desktop-fullscreen) .desktop-titlebar { display: none; }

.shell {
  flex: 1;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-rows: auto minmax(0, 1fr);
  height: 100%;
  min-width: 0;
  min-height: 0;
  background: var(--surface-app);
}

.shell--full,
.shell--collapsed {
  grid-template-columns: minmax(0, 1fr);
}

@media (width > 900px) {
  .shell__rail { grid-column: 1 / -1; }
}

.shell__side {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--surface-panel);
  border-right: 1px solid var(--border);
}

.shell__grip {
  position: absolute;
  top: 0;
  right: -3px;
  width: 7px;
  height: 100%;
  cursor: col-resize;
  z-index: 5;
}

.shell__grip::after {
  content: '';
  position: absolute;
  inset: 0 3px;
  background: transparent;
  transition: background var(--transition);
}

.shell__grip:hover::after,
.shell__grip--active::after {
  background: var(--accent);
}

.shell__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  background: var(--surface-app);
}

@media (max-width: 900px) {
  .shell {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr) auto;
  }

  .shell--full {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr);
  }

  .shell--full .shell__rail { display: none; }

  .shell__side {
    grid-row: 1;
    width: 100% !important;
    border-right: none;
  }

  .shell__grip {
    display: none;
  }

  .shell__main {
    display: none;
  }

  .shell--detail .shell__side {
    display: none;
  }

  .shell--detail .shell__main,
  .shell--full .shell__main {
    display: flex;
    grid-row: 1;
  }

  /* A detail view is the whole screen: its own back button is the way out. */
  .shell--detail .shell__rail {
    display: none;
  }

  .shell__rail {
    grid-row: 2;
  }

}
</style>
