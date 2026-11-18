<template>
  <nav class="agents" :class="{ 'agents--embedded': embedded }" aria-label="Chats by agent">
    <div class="agents__list">
      <button class="agents__item agents__all" type="button" aria-label="Show all chats" aria-describedby="agent-activity-all" :aria-pressed="!selectedId" @click="select(null)">
        <AgentAvatar icon="forum" /><span class="sr-only">All</span>
        <AgentActivity :counts="store.chatActivity" description-id="agent-activity-all" />
        <q-tooltip anchor="bottom middle" self="top middle" :offset="[0, 6]">{{ `All · ${store.chats.length} chats · ${activityLabel(store.chatActivity)}` }}</q-tooltip>
      </button>
      <div class="agents__divider" aria-hidden="true" />
      <div class="agents__people">
        <button v-for="agent in agents" :key="agent.id" class="agents__item" type="button" :aria-label="`Chats with ${agent.name}`" :aria-describedby="`agent-activity-${agent.id}`" :aria-pressed="selectedId === agent.id" @click="select(agent.id)" @keydown="openAgentMenu($event, agent.id)">
          <AgentAvatar :agent="agent" /><span class="sr-only">{{ agent.name }}</span>
          <AgentActivity :counts="agentActivity(store.chatActivity, agent.id)" :description-id="`agent-activity-${agent.id}`" />
          <q-tooltip anchor="bottom middle" self="top middle" :offset="[0, 6]">{{ agent.name }} · {{ activityLabel(agentActivity(store.chatActivity, agent.id)) }}</q-tooltip>
          <q-menu :ref="menu => registerMenu(agent.id, menu)" context-menu class="pick-menu" @before-show="openAgentMenus.add(agent.id)" @hide="openAgentMenus.delete(agent.id)">
            <RouterLink v-close-popup class="pick-menu__item agents__menu-link" :to="editorLocation(agent)"><span class="material-icons" aria-hidden="true">edit</span>Edit agent</RouterLink>
            <RouterLink v-close-popup class="pick-menu__item agents__menu-link" :to="editorLocation(agent, true)"><span class="material-icons" aria-hidden="true">add_photo_alternate</span>Change image</RouterLink>
          </q-menu>
        </button>
        <div v-if="!agents.length && !store.agentNavigation.loaded && !store.agentNavigation.error" class="agents__placeholders" role="status" aria-label="Loading agents">
          <span v-for="slot in 3" :key="slot" class="agents__placeholder" aria-hidden="true"><span class="material-icons">person</span></span>
        </div>
        <button v-if="store.agentNavigation.error" class="agents__status caption dim" type="button" aria-label="Retry loading agents" title="Retry loading agents" @click="actions.loadAgents()"><span class="material-icons" aria-hidden="true">refresh</span></button>
      </div>
    </div>
    <RouterLink class="agents__manage" to="/settings/agents#agent-profiles" aria-label="Manage agents" title="Manage agents"><span class="material-icons" aria-hidden="true">person_add_alt</span></RouterLink>
  </nav>
</template>

<script setup>
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { actions, store } from '../store'
import { chatLocation } from '../router'
import AgentAvatar from './AgentAvatar.vue'
import AgentActivity from './AgentActivity.vue'
import { agentActivity, activityLabel } from '../agent-activity'

defineProps({ embedded: Boolean })
const route = useRoute(), router = useRouter()
const agents = computed(() => store.agentNavigation.agents)
const selectedId = computed(() => typeof route.query.agent === 'string' ? route.query.agent : null)
const openAgentMenus = new Set()
function select (id) {
  // Touch release can synthesize a click after a long-press opens the menu.
  if (openAgentMenus.has(id)) return
  router.push(chatLocation('', id))
}
const agentMenus = new Map()
function registerMenu (id, menu) {
  if (menu) agentMenus.set(id, menu)
  else { agentMenus.delete(id); openAgentMenus.delete(id) }
}
function openAgentMenu (event, id) {
  if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return
  event.preventDefault()
  agentMenus.get(id)?.show(event)
}
function editorLocation (agent, image = false) {
  return { path: '/settings/agents', query: { edit: agent.id }, hash: image ? '#profile-image' : '#agent-profiles' }
}
// A removed or invalid selection must not leave the user in an empty dead end.
watch([agents, () => store.agentNavigation.loaded, selectedId], () => {
  if (store.agentNavigation.loaded && selectedId.value && !agents.value.some(agent => agent.id === selectedId.value)) {
    router.replace(chatLocation(route.params.id || '', null))
  }
}, { immediate: true })
</script>

<style scoped>
.agents { display: flex; width: 100%; min-width: 0; padding: calc(4px + env(safe-area-inset-top)) max(6px, env(safe-area-inset-right)) 4px max(6px, env(safe-area-inset-left)); border-bottom: 1px solid var(--border); background: var(--surface-panel); }
.agents--embedded { margin-top: 12px; padding: 0; border: 0; }
.agents__list { display: flex; align-items: center; flex: 1; min-width: 0; gap: 4px; }
.agents__people { display: flex; align-items: center; gap: 4px; overflow-x: auto; min-width: 0; scrollbar-width: thin; }
.agents__item { position: relative; display: grid; place-items: center; flex: none; width: 44px; height: 48px; padding: 4px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); font: inherit; cursor: pointer; --agent-avatar-size: 36px; }
.agents--embedded .agents__item { width: 40px; height: 44px; --agent-avatar-size: 32px; }
.agents__divider { flex: none; width: 1px; height: 24px; margin: 0 3px; background: var(--border-strong); }
.agents__item::before { content: ''; position: absolute; bottom: 0; width: 0; height: 3px; border-radius: var(--radius-pill); background: var(--text-muted); transition: width var(--transition); }
.agents__item:hover::before { width: 12px; }
.agents__item:hover, .agents__manage:hover { background: var(--surface-hover); color: var(--text); }
.agents__item[aria-pressed="true"] { background: var(--accent-soft); color: var(--accent); }
.agents__item[aria-pressed="true"]::before { width: 24px; background: var(--accent); }
.agents__item:focus-visible, .agents__manage:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.agents__manage { display: grid; place-items: center; flex: none; min-height: 44px; width: 44px; margin-inline-start: 4px; border-radius: var(--radius-sm); color: var(--text-dim); text-decoration: none; }
.agents__manage .material-icons { font-size: 1.25rem; }
.agents__menu-link { text-decoration: none; }
.agents__menu-link .material-icons { font-size: 1.125rem; }
.agents__placeholders { display: flex; gap: 4px; overflow: hidden; }
.agents__placeholder { display: grid; place-items: center; flex: none; width: 36px; height: 36px; margin: 6px 4px; border-radius: var(--radius-sm); background: var(--surface-raised); color: var(--text-dim); }
.agents__status { flex: none; padding: 8px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); cursor: pointer; }
.agents__status:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
