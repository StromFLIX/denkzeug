<template>
  <nav class="agents" aria-label="Chats by agent">
    <button ref="allButton" class="agents__item agents__all" type="button" aria-label="Show all chats" aria-describedby="agent-activity-all" :aria-pressed="!selectedId" @click="select(null)">
      <AgentAvatar icon="forum" /><span class="sr-only">All</span>
      <AgentActivity :counts="store.chatActivity" description-id="agent-activity-all" />
      <q-tooltip anchor="bottom middle" self="top middle" :offset="[0, 6]">{{ `All · ${store.chats.length} chats · ${activityLabel(store.chatActivity)}` }}</q-tooltip>
    </button>
    <div class="agents__divider" aria-hidden="true" />
    <div class="agents__people">
      <div v-for="row in agentRows" :key="row.key" :id="row.key === 'overflow' ? overflowId : undefined" :ref="row.key === 'primary' ? setPrimaryRow : undefined" v-show="row.key === 'primary' || (expanded && hiddenCount)" class="agents__row" :class="`agents__row--${row.key}`">
        <button v-for="agent in row.agents" :key="agent.id" class="agents__item" type="button" :aria-label="`Chats with ${agent.name}`" :aria-describedby="`agent-activity-${agent.id}`" :aria-pressed="selectedId === agent.id" @click="select(agent.id)" @keydown="openAgentMenu($event, agent.id)">
          <AgentAvatar :agent="agent" /><span class="sr-only">{{ agent.name }}</span>
          <AgentActivity :counts="agentActivity(store.chatActivity, agent.id)" :description-id="`agent-activity-${agent.id}`" />
          <q-tooltip anchor="bottom middle" self="top middle" :offset="[0, 6]">{{ agent.name }} · {{ activityLabel(agentActivity(store.chatActivity, agent.id)) }}</q-tooltip>
          <q-menu :ref="menu => registerMenu(agent.id, menu)" context-menu class="pick-menu" @before-show="openAgentMenus.add(agent.id)" @hide="openAgentMenus.delete(agent.id)">
            <RouterLink v-close-popup class="pick-menu__item agents__menu-link" :to="editorLocation(agent)"><span class="material-icons" aria-hidden="true">edit</span>Edit agent</RouterLink>
            <RouterLink v-close-popup class="pick-menu__item agents__menu-link" :to="editorLocation(agent, true)"><span class="material-icons" aria-hidden="true">add_photo_alternate</span>Change image</RouterLink>
          </q-menu>
        </button>
        <template v-if="row.key === 'primary'">
          <button v-if="hiddenCount" class="agents__item agents__more" type="button" :aria-controls="overflowId" :aria-expanded="expanded" :aria-label="expanded ? 'Show fewer agents' : `Show ${hiddenCount} more ${hiddenCount === 1 ? 'agent' : 'agents'}`" @click="expanded = !expanded">
            <span v-if="expanded" class="material-icons" aria-hidden="true">expand_less</span>
            <span v-else aria-hidden="true">+{{ hiddenCount }}</span>
          </button>
          <div v-if="!agents.length && !store.agentNavigation.loaded && !store.agentNavigation.error" class="agents__placeholders" role="status" aria-label="Loading agents">
            <span v-for="slot in 3" :key="slot" class="agents__placeholder" aria-hidden="true"><span class="material-icons">person</span></span>
          </div>
        </template>
      </div>
    </div>
    <button v-if="store.agentNavigation.error" class="agents__status caption dim" type="button" aria-label="Retry loading agents" title="Retry loading agents" @click="actions.loadAgents()"><span class="material-icons" aria-hidden="true">refresh</span></button>
    <RouterLink class="agents__manage" to="/settings/agents#agent-profiles" aria-label="Manage agents" title="Manage agents"><span class="material-icons" aria-hidden="true">person_add_alt</span></RouterLink>
  </nav>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, useId, useTemplateRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { actions, store } from '../store'
import { chatLocation } from '../router'
import AgentAvatar from './AgentAvatar.vue'
import AgentActivity from './AgentActivity.vue'
import { agentActivity, activityLabel } from '../agent-activity'
import { preferences } from '../preferences'

const route = useRoute(), router = useRouter()
const agents = computed(() => {
  const usage = store.agentUsage7d
  return [...store.agentNavigation.agents].sort((a, b) => (
    (preferences.agentOrder === 'usage' ? (usage?.[b.id] ?? 0) - (usage?.[a.id] ?? 0) : 0)
    || a.name.localeCompare(b.name) || a.id.localeCompare(b.id)
  ))
})
const selectedId = computed(() => typeof route.query.agent === 'string' ? route.query.agent : null)
const allButton = useTemplateRef('allButton')
const overflowId = useId()
const capacity = ref(1)
const expanded = ref(false)
const hiddenCount = computed(() => agents.value.length > capacity.value ? agents.value.length - capacity.value + 1 : 0)
const agentRows = computed(() => {
  const shown = hiddenCount.value ? capacity.value - 1 : agents.value.length
  const primary = agents.value.slice(0, shown)
  const overflow = agents.value.slice(shown)
  // Keep the active filter in view when collapsed, without moving agents as you select them in the expanded rows.
  const selected = overflow.findIndex(agent => agent.id === selectedId.value)
  if (!expanded.value && shown && selected >= 0) primary[shown - 1] = overflow[selected]
  return [{ key: 'primary', agents: primary }, { key: 'overflow', agents: expanded.value ? overflow : [] }]
})
watch(hiddenCount, count => { if (!count) expanded.value = false })
let primaryRow, observer
function setPrimaryRow (element) { primaryRow = element }
onMounted(() => {
  observer = new ResizeObserver(() => {
    const width = primaryRow?.clientWidth
    const itemWidth = allButton.value?.offsetWidth
    if (!width || !itemWidth) return // Sidebar is hidden; keep the last measured capacity.
    const gap = parseFloat(getComputedStyle(primaryRow).columnGap) || 0
    capacity.value = Math.max(1, Math.floor((width + gap) / (itemWidth + gap)))
  })
  observer.observe(primaryRow)
  observer.observe(allButton.value)
})
onUnmounted(() => observer?.disconnect())
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
.agents { --agent-row-height: 44px; display: flex; align-items: flex-start; gap: 4px; width: 100%; min-width: 0; margin-top: 12px; background: var(--surface-panel); }
.agents__people { flex: 1; min-width: 0; }
.agents__row { display: flex; align-items: center; gap: 4px; }
.agents__row--overflow { flex-wrap: wrap; align-content: flex-start; margin-top: 4px; max-height: calc(2 * var(--agent-row-height) + 4px); overflow-y: auto; scrollbar-width: thin; }
.agents__item { position: relative; display: grid; place-items: center; flex: none; width: 40px; height: var(--agent-row-height); padding: 4px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); font: inherit; cursor: pointer; --agent-avatar-size: 32px; }
.agents__more { font-size: 0.8125rem; font-weight: 600; }
.agents__more .material-icons { font-size: 1.25rem; }
@media (max-width: 900px) {
  .agents { --agent-row-height: 48px; }
  .agents__item { width: 44px; --agent-avatar-size: 36px; }
}
@media (max-height: 480px) {
  .agents__row--overflow { max-height: var(--agent-row-height); }
}
.agents__divider { flex: none; width: 1px; height: 24px; margin: calc((var(--agent-row-height) - 24px) / 2) 3px 0; background: var(--border-strong); }
.agents__item[aria-pressed]::before { content: ''; position: absolute; bottom: 0; width: 0; height: 3px; border-radius: var(--radius-pill); background: var(--text-muted); transition: width var(--transition); }
.agents__item[aria-pressed]:hover::before { width: 12px; }
.agents__item:hover, .agents__manage:hover { background: var(--surface-hover); color: var(--text); }
.agents__item[aria-pressed="true"] { background: var(--accent-soft); color: var(--accent); }
.agents__item[aria-pressed="true"]::before { width: 24px; background: var(--accent); }
.agents__item:focus-visible, .agents__manage:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.agents__manage { display: grid; place-items: center; flex: none; height: var(--agent-row-height); width: 44px; border-radius: var(--radius-sm); color: var(--text-dim); text-decoration: none; }
.agents__manage .material-icons { font-size: 1.25rem; }
.agents__menu-link { text-decoration: none; }
.agents__menu-link .material-icons { font-size: 1.125rem; }
.agents__placeholders { display: flex; gap: 4px; overflow: hidden; }
.agents__placeholder { display: grid; place-items: center; flex: none; width: 36px; height: 36px; margin: 6px 4px; border-radius: var(--radius-sm); background: var(--surface-raised); color: var(--text-dim); }
.agents__status { flex: none; padding: 8px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); cursor: pointer; }
.agents__status:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
