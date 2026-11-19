<template>
  <nav class="rail" data-skin-part="navigation" aria-label="Main navigation">
    <div class="rail__start">
      <RouterLink v-if="!mobile" to="/chats" class="rail__brand" :aria-label="brand.name" :title="brand.name" @click="$emit('open-sidebar')">
        <BrandMark />
      </RouterLink>
      <div class="rail__nav">
        <RouterLink
          v-for="item in items" :key="item.name" class="rail__item"
          :to="sidebarCollapsed && active === item.name ? route.fullPath : item.to"
          :class="{ 'rail__item--active': active === item.name }" :style="{ '--nav-color': item.tone }" :aria-current="active === item.name ? 'page' : undefined"
          @click="$emit('open-sidebar')"
        >
          <span class="material-icons" aria-hidden="true">{{ item.icon }}</span>
          <span class="rail__label">{{ item.label }}</span>
          <span v-if="item.badge" class="rail__badge">{{ item.badge }}</span>
        </RouterLink>
        <RouterLink v-if="!mobile" class="rail__item rail__settings" :class="{ 'rail__item--active': active === 'settings' }"
          :aria-current="active === 'settings' ? 'page' : undefined" :to="`/settings/${account.user?.role === 'admin' && health === 'degraded' ? 'system' : 'account'}`"
          :title="health === 'degraded' ? 'Settings — something needs attention' : 'Settings'" aria-label="Settings">
          <span class="material-icons" aria-hidden="true">settings</span>
          <span class="rail__label">Settings</span>
          <span v-if="health !== 'ok'" class="dot rail__settings-dot" :class="{ 'dot--bad': health === 'degraded' }" />
        </RouterLink>
      </div>
    </div>

    <WorkspaceSearchButton v-if="!mobile" class="rail__search" label="Search workspace" scope="all" :shortcut="searchShortcut"
      aria-keyshortcuts="Meta+K Control+K" />
    <button v-if="!mobile" type="button" class="btn btn--primary rail__new" aria-label="New chat" title="New chat" :disabled="route.params.id === 'new'"
      @click="router.push(chatLocation('new'))">
      <span class="material-icons" aria-hidden="true">add</span><span class="rail__label">New chat</span>
    </button>
  </nav>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { draftCount, health, store } from '../store'
import BrandMark from './BrandMark.vue'
import WorkspaceSearchButton from './WorkspaceSearchButton.vue'
import { account } from '../auth-session.js'
import { brand } from '../brand'
import { chatLocation } from '../router'

// Reopening the current list should not leave its detail view or discard a draft.
defineProps({ mobile: Boolean, sidebarCollapsed: Boolean })
defineEmits(['open-sidebar'])

const searchShortcut = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl K'
const route = useRoute()
const router = useRouter()
const active = computed(() => route.meta.section || route.name)
const items = computed(() => [
  { name: 'chats', label: 'Chats', icon: 'chat_bubble_outline', tone: 'var(--agent-color)', to: chatLocation() },
  { name: 'workflows', label: 'Workflows', icon: 'account_tree', tone: 'var(--workflow-color)', to: '/workflows', badge: draftCount.value || 0 }
].filter(item => account.user?.role === 'admin' || item.name === 'chats' ||
  (item.name === 'workflows' && (store.workflows.length || store.runs.length))))
</script>

<style scoped>
.rail {
  display: grid;
  grid-template-columns: minmax(max-content, 1fr) minmax(180px, 30vw) minmax(max-content, 1fr);
  align-items: center;
  gap: 16px;
  min-width: 0;
  padding: 8px max(16px, env(safe-area-inset-right)) 8px max(16px, env(safe-area-inset-left));
  background: var(--surface-panel);
  border-bottom: 1px solid var(--border);
}
.rail__start, .rail__nav { display: flex; align-items: center; gap: 4px; }
.rail__start { gap: 10px; }
.rail__search { width: 100%; max-width: 420px; justify-self: center; }
.rail__new { justify-self: end; gap: 6px; white-space: nowrap; }
.rail__brand { display: grid; place-items: center; flex: none; width: 40px; height: 40px; border-radius: var(--radius-md); }
.rail__brand :deep(.brand-mark) { width: 30px; height: 30px; }

.rail__item {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 44px;
  padding-inline: 10px;
  white-space: nowrap;
  border: none;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-dim);
  font: inherit;
  cursor: pointer;
  text-decoration: none;
  transition: background var(--transition), color var(--transition);
}
.rail__item:hover { background: var(--surface-hover); color: var(--text); }
.rail__item--active { background: color-mix(in srgb, var(--nav-color, var(--accent)) 9%, transparent); color: var(--nav-color, var(--accent)); }
.rail__settings-dot { position: absolute; top: 4px; right: 3px; border: 2px solid var(--surface-panel); }
.rail__label { font-size: 0.75rem; font-weight: 500; }
.rail__badge {
  min-width: 1rem;
  height: 1rem;
  text-align: center;
  padding: 0 4px;
  border-radius: var(--radius-pill);
  background: var(--warning);
  color: var(--warning-text);
  font-size: 0.625rem;
  font-weight: 700;
  line-height: 1rem;
}

@media (900px < width <= 1040px) {
  .rail { gap: 10px; padding-inline: 12px; }
  .rail__start { gap: 6px; }
  .rail__item { padding-inline: 7px; }
  .rail__item > .material-icons { display: none; }
}
@media (max-width: 900px) {
  .rail {
    display: flex;
    align-items: stretch;
    gap: 0;
    height: calc(var(--mobile-nav-height) + env(safe-area-inset-bottom));
    padding: 4px max(4px, env(safe-area-inset-right)) max(4px, env(safe-area-inset-bottom)) max(4px, env(safe-area-inset-left));
    background: var(--surface-rail);
    border-bottom: 0;
    border-top: 1px solid var(--border);
  }
  .rail__start { display: contents; }
  .rail__nav { flex: 1 1 auto; align-items: stretch; gap: 2px; min-width: 0; }
  .rail__item { flex: 1 1 0; flex-direction: column; min-width: 0; min-height: 3.125rem; padding-inline: 0; border-radius: var(--radius-sm); }
  .rail__label { font-size: 0.625rem; letter-spacing: 0.01em; }
  .rail__badge { position: absolute; top: 4px; right: max(8px, calc(50% - 24px)); }
}
</style>
