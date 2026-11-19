<template>
  <div class="side stack grow">
    <header class="side__head">
      <div class="row side__heading">
        <h1 class="side__title grow truncate" :title="heading">{{ heading }}</h1>
        <button v-if="section !== 'chats'" class="btn btn--icon" title="Refresh" @click="refresh">
          <span class="material-icons">refresh</span>
        </button>
      </div>
      <WorkspaceSearchButton v-if="mobile" class="side__search" :scope="section" :label="`Search ${section}`" />
      <slot name="agents" />
    </header>

    <div class="side__list scroll-y grow" :class="{ 'side__list--chats': section === 'chats', 'side__list--grouped': section === 'chats' && groupBy !== 'none' }">
      <!-- chats -->
      <template v-if="section === 'chats'">
        <div v-for="group in chatGroups" :key="group.key" class="side__chat-group">
          <h2 v-if="group.key !== '__all__'" class="side__group side__group--chats">
            <span class="truncate" :class="{ 'side__group-date': groupBy === 'date' }" :title="group.label">{{ group.label }}</span>
            <span class="side__group-count">{{ group.chats.length }} {{ group.chats.length === 1 ? 'chat' : 'chats' }}</span>
          </h2>

          <ChatTree v-for="chat in group.chats" :key="chat.id" :node="chat.treeNode"
            :expanded="expandedChats"
            :active-route-id="route.params.id" :read-busy="readBusy"
            @toggle-tree="toggleChatTree" @toggle-unread="setUnread" />
        </div>

        <button
          v-if="hiddenCount" type="button" class="side__more"
          @click="showOlder = !showOlder"
        >
          <span class="material-icons" aria-hidden="true">{{ showOlder ? 'expand_less' : 'expand_more' }}</span>
          {{ showOlder ? `Hide ${hiddenCount} older` : `Show ${hiddenCount} older` }}
        </button>
        <p v-if="readError" class="side__error caption" role="alert">{{ readError }}</p>
        <p v-if="!filteredChats.length" class="side__empty caption">
          No chats yet.
        </p>
        <p v-else-if="!visibleCount && !showOlder" class="side__empty caption">
          Nothing active in the last {{ activeLabel }}.
          <button type="button" class="side__empty-link" @click="showOlder = true">Show older chats</button>
        </p>
      </template>

      <!-- workflows -->
      <template v-else-if="section === 'workflows'">
        <RouterLink to="/workflows?view=runs" class="row-item" :class="{ 'row-item--active': route.query.view === 'runs' }"><span class="material-icons" aria-hidden="true">history</span><span>All runs</span></RouterLink>
        <template v-if="store.drafts.length">
          <div class="side__group section-label">Waiting for review</div>
          <RouterLink
            v-for="draft in store.drafts" :key="draft.name" :to="`/workflows/${draft.name}`"
            class="row-item" :class="{ 'row-item--active': route.params.name === draft.name }"
          >
            <div class="avatar avatar--square avatar--draft">
              <span class="material-icons">rate_review</span>
            </div>
            <div class="grow">
              <div class="row-item__title truncate">{{ draft.name }}</div>
              <div class="row-item__sub truncate">{{ draft.meta?.message || draft.description || 'Draft workflow' }}</div>
            </div>
            <span class="chip chip--warning">draft</span>
          </RouterLink>
          <div class="side__group section-label">Published</div>
        </template>

        <RouterLink
          v-for="workflow in workflowRows" :key="workflow.name" :to="`/workflows/${workflow.name}`"
          class="row-item" :class="{ 'row-item--active': route.params.name === workflow.name }"
        >
          <div class="avatar avatar--square avatar--workflow">
            <span class="material-icons">account_tree</span>
          </div>
          <div class="grow">
            <div class="row">
              <span class="row-item__title grow truncate">{{ workflow.title || workflow.name }}</span>
              <span v-if="workflow.settings?.disabled" class="material-icons row-item__pin dim">pause_circle</span>
              <span v-else-if="workflow.scheduleCount" class="material-icons row-item__pin">schedule</span>
            </div>
            <div
              class="row-item__sub truncate"
              :title="workflow.nextSchedule ? `${workflow.nextSchedule.description} · ${workflow.nextSchedule.timezone}` : ''"
            >
              {{ workflow.nextSchedule && !workflow.settings?.disabled
                ? `Next ${scheduleTime(workflow.nextSchedule.next_run, workflow.nextSchedule.timezone)}`
                : workflow.scheduleCount ? `${workflow.scheduleCount} ${workflow.scheduleCount === 1 ? 'schedule' : 'schedules'}` : workflow.description || workflow.name }}
            </div>
          </div>
        </RouterLink>
        <p v-if="!store.workflows.length && !store.drafts.length" class="side__empty caption">No workflows yet.</p>
      </template>

    </div>

    <button v-if="mobile && section === 'chats'" type="button" class="btn btn--icon side__new"
      title="New chat" aria-label="New chat" :disabled="route.params.id === 'new'" @click="startChat">
      <svg viewBox="0 -960 960 960" aria-hidden="true" focusable="false">
        <path fill="currentColor" d="M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h357l-80 80H200v560h560v-278l80-80v358q0 33-23.5 56.5T760-120H200Zm280-360ZM360-360v-170l367-367q12-12 27-18t30-6q16 0 30.5 6t26.5 18l56 57q11 12 17 26.5t6 29.5q0 15-5.5 29.5T897-728L530-360H360Zm481-424-56-56 56 56ZM440-440h56l232-232-28-28-29-28-231 231v57Zm260-260-29-28 29 28 28 28-28-28Z" />
      </svg>
    </button>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import WorkspaceSearchButton from './WorkspaceSearchButton.vue'
import { scheduleTime } from '../format'
import { nextWorkflowSchedule, workflowSchedules } from '../workflow-schedules'
import { actions, store } from '../store'
import { api } from '../api'
import ChatTree from './ChatTree.vue'
import { preferences } from '../preferences'
import { groupChats } from '../chat-groups'
import { chatAncestors, chatForest, filterChatForest, chatTreeRows } from '../chat-tree'
import { belongsToAgent } from '../agent-config'
import { chatLocation } from '../router'

const props = defineProps({
  mobile: Boolean,
  isChatActive: { type: Function, required: true }
})

const route = useRoute()
const router = useRouter()
const readBusy = ref('')
const readError = ref('')

const groupBy = computed(() => preferences.chatGroupBy)
const activeMinutes = computed(() => preferences.chatActiveMinutes)
const showOlder = ref(false)

watch(activeMinutes, () => { showOlder.value = false })

const activeLabel = computed(() =>
  ({ 60: 'hour', 1440: '24 hours', 10080: '7 days', 43200: '30 days' }[activeMinutes.value] || 'selected range'))

const expandedChats = ref(new Set())
function toggleChatTree (id) {
  const next = new Set(expandedChats.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedChats.value = next
}

// Reveal the selected conversation on direct navigation, but let users collapse
// it again. Polling the same ancestry must not undo their disclosure choice.
watch(() => JSON.stringify([route.params.id, chatAncestors(store.chats, route.params.id)]), () => {
  expandedChats.value = new Set([...expandedChats.value, ...chatAncestors(store.chats, route.params.id)])
}, { immediate: true })

function isTreeActive (chat) {
  return chatTreeRows(chat.treeNode, new Set(), true).some(node => props.isChatActive(node.chat))
}

const chatGroups = computed(() =>
  groupChats(filteredChats.value, groupBy.value, store.projects)
    .map(group => ({ ...group, chats: group.chats.filter(chat => showOlder.value || isTreeActive(chat)) }))
    .filter(group => group.chats.length))

const visibleCount = computed(() => filteredChats.value.filter(isTreeActive).length)
const hiddenCount = computed(() => filteredChats.value.length - visibleCount.value)

async function setUnread (chat, unread) {
  readBusy.value = chat.id
  readError.value = ''
  try {
    // Leave the open chat first so it is not immediately acknowledged again.
    if (unread && route.params.id === chat.id) await router.push(chatLocation())
    await api.updateChatReadState(chat.id, { unread })
    await actions.loadChats()
  } catch (error) {
    readError.value = `Could not update read status: ${error.message}`
  } finally {
    readBusy.value = ''
  }
}

const section = computed(() => route.meta.section || route.name || 'chats')
const selectedAgent = computed(() => section.value === 'chats' ? store.agentNavigation.agents.find(agent => agent.id === route.query.agent) : null)
const heading = computed(() => selectedAgent.value?.name || ({ chats: 'Chats', workflows: 'Workflows', runs: 'Runs' }[section.value]))

const filteredChats = computed(() =>
  filterChatForest(chatForest(store.chats), chat => belongsToAgent(chat, route.query.agent))
    .map(treeNode => ({ ...treeNode.summary, treeNode })))

const workflowRows = computed(() => store.workflows.map(workflow => {
  const schedules = workflowSchedules(workflow)
  return { ...workflow, nextSchedule: nextWorkflowSchedule(schedules), scheduleCount: schedules.length }
}))

async function startChat () {
  // Keep the list beneath the draft even if clicked during initial lazy routing.
  await router.isReady()
  router.push(chatLocation('new'))
}

function refresh () {
  if (section.value === 'workflows') actions.loadWorkflows()
  else actions.loadRuns()
}
</script>

<style scoped>
.side {
  overflow: hidden;
}

.side__head {
  padding: 22px 18px 16px;
  border-bottom: 1px solid var(--border);
}

.side__heading {
  gap: var(--space-1);
}

.side__title {
  margin: 0;
  font-size: 1.125rem;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: -0.045em;
}

.side__search { margin-top: 18px; }
@media (max-height: 420px) { .side__search { margin-top: 8px; } }

.side__more {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  margin-top: 8px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: 0.75rem;
  font-weight: 600;
  cursor: pointer;
}

.side__more:hover {
  color: var(--text);
  background: var(--surface-hover);
}

.side__more .material-icons {
  font-size: 1rem;
}

.side__empty-link {
  display: block;
  margin: 6px auto 0;
  border: none;
  background: none;
  color: var(--accent-hover);
  font: inherit;
  font-size: 0.75rem;
  cursor: pointer;
}

.side__list {
  padding: 10px 8px;
  /* Rows follow the panel width; long titles truncate instead of stretching. */
  min-width: 0;
  overflow-x: hidden;
  isolation: isolate;
}

.side__list--grouped {
  /* Sticky headers sit flush against the list header. */
  padding-top: 0;
}

.side__list > * {
  max-width: 100%;
}

.side__chat-group {
  min-width: 0;
}

.side__group {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 14px 10px 6px;
  position: sticky;
  top: 0;
  /* Keep the opaque heading above the positioned chat status and menu. */
  z-index: 2;
  background: var(--surface-panel);
}

.side__group--chats {
  align-items: center;
  gap: 12px;
  padding: 10px;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 0.8125rem;
  font-weight: 600;
  line-height: 1.4;
}

.side__group--chats .truncate {
  flex: 1;
  min-width: 0;
}

.side__group-date {
  /* Keep the full date readable on narrow panels and at larger text sizes. */
  white-space: normal;
}

.side__group-count {
  flex: none;
  padding: 2px 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  background: var(--surface-input);
  color: var(--text-dim);
  font-weight: 500;
  font-size: 0.6875rem;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.side__empty {
  padding: 24px 14px;
  color: var(--text-dim);
  text-align: center;
}

.side__error {
  padding: 8px 10px;
  color: var(--danger);
}

@media (max-width: 900px) {
  .side__head {
    padding-top: calc(10px + env(safe-area-inset-top));
    padding-right: max(12px, env(safe-area-inset-right));
    padding-left: max(12px, env(safe-area-inset-left));
  }

  .side__list {
    /* Scroll through the overlay, but let the final row come to rest above it. */
    --list-bottom-space: calc(var(--mobile-nav-clearance) + 10px);
    padding-right: max(6px, env(safe-area-inset-right));
    padding-left: max(6px, env(safe-area-inset-left));
    padding-bottom: var(--list-bottom-space);
    scroll-padding-block-end: var(--list-bottom-space);
  }

  .side__list--chats {
    --list-bottom-space: calc(var(--mobile-compose-bottom) + var(--mobile-compose-size) + 10px);
  }

  .side__new {
    position: absolute;
    z-index: 2;
    inset-inline-end: max(14px, env(safe-area-inset-right));
    inset-block-end: var(--mobile-compose-bottom);
    width: var(--mobile-compose-size);
    height: var(--mobile-compose-size);
    color: var(--text-muted);
    background: var(--surface-overlay);
    border-color: var(--border-strong);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-md);
    touch-action: manipulation;
  }
  .side__new svg { width: 1.15rem; height: 1.15rem; }
  .side__new:not(:disabled):hover { background: var(--surface-overlay); }
  .side__new:not(:disabled):active { color: var(--text); border-color: var(--text-muted); }

  .row-item {
    min-height: 58px;
  }
}
</style>
