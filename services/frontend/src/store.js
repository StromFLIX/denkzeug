/**
 * One shared reactive store. Every view reads the same lists, and the live
 * event stream is what keeps them fresh, so nothing polls.
 */
import { computed, reactive } from 'vue'
import { api, liveEvents, server } from './api'
import { account, clearAccount } from './auth-session.js'
import { delivery } from './delivery'
import { chatCache, chatCacheScope, warmChatCache } from './chat-cache'
import { agentConfig, globalChatConfig, resolveAgentConfig } from './agent-config'
import { createChatSettings } from './new-chat-settings'
import { createAgentNavigation } from './agent-navigation'
import { preferenceError } from './preferences'
import router from './router'
import { createMcpAuthState } from './mcp-auth-state.js'

export const chatSettings = createChatSettings({ scope: chatCacheScope, onError: message => { preferenceError.value = message } })

const agentNavigation = createAgentNavigation({ scope: () => `${chatCacheScope()}:${account.user?.role || 'signed-out'}`, fetchAgents: () => guard(() => api.agents()) })
const emptyCatalog = () => ({ agent_sets: [], agents: [], models: [], tools: [], default_model: '', default_agent_set: 'default', default_agent_id: null })
const state = reactive({
  ready: false,
  catalogLoaded: false,
  catalogError: '',
  liveStatus: 'connecting',
  system: { components: [], agent_sets: [] },
  catalog: emptyCatalog(),
  agentNavigation: agentNavigation.state,
  chats: [],
  chatActivity: null,
  projects: [],
  workflows: [],
  workflowInputDraft: null,
  drafts: [],
  runs: [],
  events: [],
  mcpAuth: { connections: [], prompt: null, busyName: '', error: '', snoozedUntil: 0 }
})
function mcpDeviceScope () {
  try {
    const url = new URL(server.url || globalThis.location.origin)
    url.username = url.password = url.search = url.hash = ''
    return url.href.replace(/\/$/, '')
  } catch { return 'local' }
}
const mcpAuth = createMcpAuthState({ state: state.mcpAuth, scope: chatCacheScope,
  deviceScope: mcpDeviceScope, storage: () => globalThis.localStorage, events: globalThis,
  onError: error => { state.mcpAuth.error = error.message },
  fetchStatus: () => guard(() => api.mcpAuthStatus()),
  rejectRequests: ids => api.dismissMcpOAuth(ids) })

const listeners = new Set()
let source = null
let catalogRequest = 0
let chatsRequest = 0
let workflowsRequest = 0
let runsRequest = 0

function emit (event) {
  listeners.forEach((fn) => fn(event))
}

export function onLiveEvent (fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

async function guard (loader) {
  try {
    return await loader()
  } catch (error) {
    if (error.status === 401) clearAccount()
    else if (!error.status) return null
    else throw error
    return null
  }
}

export const actions = {
  async loadSystem () {
    const data = await guard(() => api.system())
    if (data) state.system = data
  },

  loadAgents: () => agentNavigation.load(),

  async loadCatalog (refresh = false) {
    const scope = chatCacheScope()
    const request = ++catalogRequest
    try {
      const data = await guard(() => api.catalog(refresh))
      if (scope !== chatCacheScope() || request !== catalogRequest) return false
      if (data) { state.catalog = data; state.catalogLoaded = true; state.catalogError = ''; return true }
      state.catalogError = 'Could not load chat defaults.'
    } catch (error) { if (scope === chatCacheScope() && request === catalogRequest) state.catalogError = error.message }
    return false
  },

  // Successful writes are authoritative even if subsequent discovery is offline.
  syncAgentCatalog (patch) {
    catalogRequest++ // An older discovery response must not undo this saved edit.
    const catalog = { ...state.catalog, ...patch }
    const defaults = globalChatConfig(catalog)
    catalog.agents = (catalog.agents || []).map(agent => ({ ...agent, resolved: resolveAgentConfig(agent.config || {}, defaults) }))
    catalog.chat_defaults = agentConfig(catalog, catalog.default_agent_id ?? null)
    state.catalog = catalog
    if (patch.agents) agentNavigation.sync(patch.agents)
  },

  applyAgentSettings (settings) {
    const defaults = globalChatConfig(settings)
    actions.syncAgentCatalog({ global_chat_defaults: defaults, default_model: defaults.model,
      default_agent_set: defaults.agent_set, default_agent_id: settings.default_agent_id ?? null })
  },

  // Creation already returned the authoritative chat. Opening it must not wait
  // for another list request or for the offline cache to finish writing.
  addChat (chat, scope = chatCacheScope()) {
    if (scope !== chatCacheScope()) return
    state.chats = [chat, ...state.chats.filter(item => item.id !== chat.id)]
    chatCache.saveList(state.chats, scope)
    chatCache.put({ chat, messages: [], active_turn: null }, scope)
  },

  async loadChats () {
    const scope = chatCacheScope()
    const request = ++chatsRequest
    const data = await guard(() => api.chats())
    if (data && scope === chatCacheScope() && request === chatsRequest) {
      state.chats = data.chats
      // Activity is server-wide and never restored from the offline list cache.
      state.chatActivity = data.activity || null
      await chatCache.saveList(data.chats, scope)
      warmChatCache(data.chats, scope)
    }
  },

  async loadProjects () {
    const data = await guard(() => api.projects())
    if (data) state.projects = data.projects
  },

  async loadWorkflows () {
    const scope = chatCacheScope(), request = ++workflowsRequest
    const [workflows, drafts] = await Promise.all([
      guard(() => api.workflows()),
      account.user?.role === 'admin' ? guard(() => api.drafts()) : Promise.resolve({ drafts: [] })
    ])
    if (scope !== chatCacheScope() || request !== workflowsRequest) return
    if (workflows) state.workflows = workflows.workflows
    if (drafts) state.drafts = drafts.drafts
  },

  async loadRuns () {
    const scope = chatCacheScope(), request = ++runsRequest
    const data = await guard(() => api.runs())
    if (data && scope === chatCacheScope() && request === runsRequest) state.runs = data.runs
  },

  async loadMcpAuth () {
    if (!account.user || account.user.role !== 'admin') return
    try { await mcpAuth.refresh(); state.mcpAuth.error = '' } catch (error) { state.mcpAuth.error = error.message }
  },
  openMcpLogin: item => mcpAuth.open(item),
  dismissMcpLogin: (failed = true) => mcpAuth.dismiss(failed),
  beginMcpLogin: name => mcpAuth.begin(name),
  finishMcpLogin: name => mcpAuth.finish(name),
  snoozeMcpLogin: duration => mcpAuth.snooze(duration),
  async resumeMcpLoginReminders () {
    try { await mcpAuth.resume(); state.mcpAuth.error = '' } catch (error) { state.mcpAuth.error = error.message }
  },

  async loadEvents () {
    const data = await guard(() => api.events())
    if (data) state.events = data.events.slice().reverse()
  },

  async refreshAll () {
    if (!account.user) return
    const common = [actions.loadCatalog(), actions.loadChats()]
    if (account.user.role !== 'admin') { await Promise.all([...common, actions.loadAgents(), actions.loadProjects(), actions.loadWorkflows(), actions.loadRuns()]); state.ready = true; return }
    await Promise.all([
      ...common,
      actions.loadSystem(),
      actions.loadAgents(),
      actions.loadCatalog(),
      actions.loadChats(),
      actions.loadProjects(),
      actions.loadMcpAuth(),
      actions.loadWorkflows(),
      actions.loadRuns()
    ])
    state.ready = true
  },

  openSettings (tab = 'account') {
    return router.push(`/settings/${tab}`)
  },

  resetAccount () {
    actions.disconnect()
    catalogRequest++
    chatsRequest++
    workflowsRequest++
    runsRequest++
    state.ready = state.catalogLoaded = false
    state.catalogError = ''
    state.catalog = emptyCatalog()
    state.system = { components: [], agent_sets: [] }
    state.chats = []; state.chatActivity = null; state.projects = []
    state.workflows = []; state.workflowInputDraft = null; state.drafts = []; state.runs = []; state.events = []
    agentNavigation.reset({ restoreCached: Boolean(account.user) })
    mcpAuth.clear()
  },

  connect () {
    source?.close()
    source = null
    if (!account.user) return
    state.liveStatus = 'connecting'
    const scope = chatCacheScope()
    chatsRequest++
    state.chats = []
    state.chatActivity = null
    chatCache.list(scope).then((chats) => {
      if (scope === chatCacheScope() && !state.chats.length) state.chats = chats || []
    })
    delivery.connect()
    emit({ kind: 'client.reconnect' })
    source = liveEvents((event) => {
      if (event.account_id && event.account_id !== account.user?.id) { clearAccount(); return }
      state.events.unshift(event)
      state.events = state.events.slice(0, 200)
      const kind = event.kind || ''
      if (kind === 'connected') actions.refreshAll().catch(() => {})
      if (account.user?.role !== 'admin' && !/^(chat|run|workflow)\./.test(kind)) return
      if (kind.startsWith('run.')) actions.loadRuns().catch(() => {})
      if (kind.startsWith('workflow.') || kind.startsWith('promote.')) actions.loadWorkflows().catch(() => {})
      if (kind.startsWith('chat.')) actions.loadChats().catch(() => {})
      if (kind.startsWith('project.')) actions.loadProjects().catch(() => {})
      if (kind === 'agent.profile.changed') actions.loadAgents()
      if (['model.integration.changed', 'mcp.server.changed', 'settings.changed', 'agent.profile.changed'].includes(kind)) actions.loadCatalog(true).catch(() => {})
      if (kind.startsWith('mcp.oauth.') || kind === 'mcp.server.changed') actions.loadMcpAuth().catch(() => {})
      emit(event)
    }, error => {
      state.liveStatus = error.permanent ? 'failed' : 'reconnecting'
      if (error.status === 401) clearAccount()
    }, () => { state.liveStatus = 'connected' })
  },

  disconnect () {
    source?.close()
    source = null
    state.liveStatus = 'disconnected'
    delivery.disconnect()
  }
}

export const store = state

export const health = computed(() => {
  const components = state.system.components || []
  if (!components.length) return 'unknown'
  return components.every((component) => component.status === 'ok') ? 'ok' : 'degraded'
})

export const draftCount = computed(() => state.drafts.length)

/** What this model can actually be handed, using the backend's own arithmetic. */
export function historyBudget (modelId) {
  const context = state.catalog.context || {}
  if (context.override) return context.override
  const id = modelId || state.catalog.default_model
  const model = (state.catalog.models || []).find((item) => item.id === id)
  if (model?.context_length) {
    return Math.round(model.context_length * context.chars_per_token * context.history_share)
  }
  return context.fallback || 200000
}
