import { liveConnection } from './live-connection.js'
import { connectSpeechStream } from './speech-stream.js'
import { account, auth, clearAccount, isNative } from './auth-session.js'
import { BearerEventSource } from './bearer-events.js'
export { auth, isNative } from './auth-session.js'

let reauthenticate = null
let refreshSession = null
export function setReauthHandler (handler) { reauthenticate = handler }
export function setRefreshHandler (handler) { refreshSession = handler }

const SERVER_KEY = 'nautionette.server'

export function normalizeServerOrigin (value) {
  const trimmed = (value || '').trim().replace(/\/+$/, '')
  if (!trimmed) return ''
  let url
  try { url = new URL(trimmed) } catch { /* report the same guidance for malformed URLs */ }
  if (url?.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('Use an HTTPS server origin, without a path or credentials.')
  return url.origin
}

export const server = {
  get url () {
    return isNative ? localStorage.getItem(SERVER_KEY) || '' : ''
  },
  set url (value) {
    if (!isNative) return
    const origin = normalizeServerOrigin(value)
    if (origin) localStorage.setItem(SERVER_KEY, origin)
    else localStorage.removeItem(SERVER_KEY)
  }
}

/** Same-origin on the web, an absolute instance URL in the app. */
export function endpoint (path) {
  return `${server.url}${path}`
}

function headers (extra = {}) {
  const out = { ...extra }
  if (isNative && auth.token) out.Authorization = `Bearer ${auth.token}`
  return out
}

export class ApiError extends Error {
  constructor (message, status, detail = null) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

export async function request (path, options = {}, retried = 0) {
  const epoch = account.epoch
  const origin = endpoint('')
  const method = options.method || 'GET'
  const csrf = !isNative && !['GET', 'HEAD', 'OPTIONS'].includes(method) ? { 'X-CSRF-Token': account.csrf } : {}
  const response = await fetch(endpoint(path), {
    credentials: isNative ? 'omit' : 'same-origin',
    ...options,
    headers: headers({ ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...csrf, ...options.headers })
  })
  if (epoch !== account.epoch || origin !== endpoint('')) throw new ApiError('Account changed. Please try again.', 409)
  const responseAccount = response.headers.get('x-denkzeug-account')
  if (account.user && responseAccount && responseAccount !== account.user.id) {
    clearAccount()
    throw new ApiError('Account changed in another tab. Sign in again.', 409)
  }
  if (response.status === 401 && !responseAccount && isNative && auth.token && !(retried & 1) && refreshSession && !/^\/api\/auth\/(refresh|login|bootstrap|register|forgot-password|reset-password|verify-email)(?:\?|$)/.test(path)) {
    await refreshSession()
    return request(path, options, retried | 1)
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`, structured = null
    try {
      const body = await response.json()
      if (body?.detail) {
        structured = body.detail
        detail = typeof body.detail === 'string' ? body.detail : Array.isArray(body.detail) ? body.detail.map(item => item.msg || 'Invalid request.').join(' ') : body.detail.message || 'The request could not be completed.'
      }
    } catch { /* keep the status line */ }
    if (epoch !== account.epoch || origin !== endpoint('')) throw new ApiError('Account changed. Please try again.', 409)
    if (response.status === 403 && structured?.code === 'reauthentication_required' && reauthenticate && !(retried & 2)) {
      await reauthenticate()
      if (epoch !== account.epoch || origin !== endpoint('')) throw new ApiError('Account changed.', 409)
      return request(path, options, retried | 2)
    }
    if (response.status === 401 && !responseAccount && account.user && !/^\/api\/auth\/(reauth|refresh)(?:\?|$)/.test(path)) clearAccount()
    throw new ApiError(detail, response.status, structured)
  }
  if (response.status === 204) return null
  const data = await (options.responseType === 'blob' ? response.blob() : response.json())
  if (epoch !== account.epoch || origin !== endpoint('')) throw new ApiError('Account changed. Please try again.', 409)
  return data
}

const json = (body) => ({ body: JSON.stringify(body ?? {}) })

export const api = {
  health: () => request('/healthz', { signal: AbortSignal.timeout(5000) }),
  speech: () => request('/api/speech'),
  async streamDictation (language, { signal, onEvent }) {
    const { ticket } = await request('/api/speech/stream-ticket', { method: 'POST', ...json({ language }), signal })
    const url = new URL(endpoint('/api/speech/stream'), globalThis.location.href)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    return connectSpeechStream(url.href, ticket, { signal, onEvent })
  },
  system: () => request('/api/system'),
  catalog: (refresh = false) => request(`/api/catalog${refresh ? '?refresh=true' : ''}`),
  events: () => request('/api/events/recent'),
  tokenCatalog: () => request('/api/tokens/catalog'),
  tokens: () => request('/api/tokens'),
  createToken: payload => request('/api/tokens', { method: 'POST', ...json(payload) }),
  rotateToken: id => request(`/api/tokens/${encodeURIComponent(id)}/rotate`, { method: 'POST' }),
  revokeToken: id => request(`/api/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  tokenRequests: (id, offset = 0, limit = 50) => request(`/api/tokens/${encodeURIComponent(id)}/requests?${new URLSearchParams({ offset, limit })}`),
  tokenUsage: id => request(`/api/tokens/${encodeURIComponent(id)}/usage`),
  // Omitted days keep the short-lived default; explicit null means never expires.
  workflowWebhookToken: (name, days = 7) => request(`/api/workflows/${encodeURIComponent(name)}/webhook-token`, { method: 'POST', ...json({ days }) }),
  settings: () => request('/api/settings'),
  saveSettings: (payload) => request('/api/settings', { method: 'PUT', ...json(payload) }),
  usage: (params = {}, signal) => request(`/api/settings/usage?${new URLSearchParams(params)}`, { signal }),
  exportUsage: (params = {}, signal) => request(`/api/settings/usage/export?${new URLSearchParams(params)}`, { responseType: 'blob', signal }),
  worktreeRetention: () => request('/api/settings/worktrees'),
  previewWorktreeCleanup: () => request('/api/settings/worktrees/cleanup', { method: 'POST', ...json({ dry_run: true }) }),
  outputStorage: (signal) => request('/api/settings/storage', { signal }),
  saveOutputStorage: (payload) => request('/api/settings/storage', { method: 'PUT', ...json(payload) }),
  storedOutputs: (params = {}, signal) => request(`/api/settings/storage/outputs?${new URLSearchParams(params)}`, { signal }),
  previewOutputCleanup: (payload) => request('/api/settings/storage/cleanup/preview', { method: 'POST', ...json(payload) }),
  deleteStoredOutputs: (planId) => request('/api/settings/storage/cleanup', { method: 'POST', ...json({ plan_id: planId, confirm: true }) }),
  maintainOutputStorage: () => request('/api/settings/storage/maintenance', { method: 'POST' }),
  compactOutputDatabase: () => request('/api/settings/storage/compact', { method: 'POST' }),
  agents: () => request('/api/agents'),
  agent: (id) => request(`/api/agents/${encodeURIComponent(id)}`),
  searchPackages: (query, offset = 0, signal) => request(`/api/pi-packages/search?q=${encodeURIComponent(query)}&offset=${offset}`, { signal }),
  apps: () => request('/api/apps'),
  app: id => request(`/api/apps/${encodeURIComponent(id)}`),
  appRevision: (id, revision) => request(`/api/apps/${encodeURIComponent(id)}/revisions/${encodeURIComponent(revision)}`),
  publishApp: payload => request('/api/apps', { method: 'POST', ...json(payload) }),
  editApp: (id, payload) => request(`/api/apps/${encodeURIComponent(id)}`, { method: 'PATCH', ...json(payload) }),
  creativeApps: () => request('/api/artcraft/apps'),
  checkCreativeAppUpdates: () => request('/api/artcraft/updates/check', { method: 'POST' }),
  updateAllCreativeApps: () => request('/api/artcraft/updates/apply', { method: 'POST' }),
  configureCreativeAppUpdates: (payload) => request('/api/artcraft/updates', { method: 'PUT', ...json(payload) }),
  creativeAppReleases: (app) => request(`/api/artcraft/apps/${encodeURIComponent(app)}/releases?refresh=true`),
  configureCreativeApp: (app, payload) => request(`/api/artcraft/apps/${encodeURIComponent(app)}`, { method: 'PUT', ...json(payload) }),
  packageInstallations: () => request('/api/pi-packages/installations'),
  installPackage: (payload) => request('/api/pi-packages/installations', { method: 'POST', ...json(payload) }),
  configurePackage: (id, payload) => request(`/api/pi-packages/installations/${encodeURIComponent(id)}/configuration`, { method: 'PATCH', ...json(payload) }),
  packageRevision: (id) => request(`/api/pi-packages/revisions/${encodeURIComponent(id)}`),
  createPackageRevision: (payload) => request('/api/pi-packages/revisions', { method: 'POST', ...json(payload) }),
  createAgent: (payload) => request('/api/agents', { method: 'POST', ...json(payload) }),
  updateAgent: (id, payload) => request(`/api/agents/${encodeURIComponent(id)}`, { method: 'PATCH', ...json(payload) }),
  deleteAgent: (id) => request(`/api/agents/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  modelIntegrations: () => request('/api/model-integrations'),
  addModelIntegration: (id, config = {}) =>
    request(`/api/model-integrations/${encodeURIComponent(id)}`, { method: 'PUT', ...json(config) }),
  removeModelIntegration: (id) =>
    request(`/api/model-integrations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  testModelIntegration: (id) =>
    request(`/api/model-integrations/${encodeURIComponent(id)}/test`, { method: 'POST' }),
  startCopilotLogin: (config = {}) =>
    request('/api/model-integrations/copilot/login', { method: 'POST', ...json(config) }),
  pollCopilotLogin: (id) =>
    request(`/api/model-integrations/copilot/login/${encodeURIComponent(id)}`, { method: 'POST' }),
  cancelCopilotLogin: (id) =>
    request(`/api/model-integrations/copilot/login/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  mcpAuthStatus: () => request('/api/mcp-oauth/status'),
  dismissMcpOAuth: requests => request('/api/mcp-oauth/dismiss', { method: 'POST', ...json({ requests }) }),
  prepareMcpOAuth: (name, config = {}) => request(`/api/mcp-oauth/prepare/${encodeURIComponent(name)}`, { method: 'POST', ...json(config) }),
  connectMcpServer: (name, config) => request(`/api/mcp-oauth/connect/${encodeURIComponent(name)}`, { method: 'POST', ...json(config) }),
  mcpOAuthCapabilities: () => request('/api/mcp-oauth/capabilities'),
  discoverMcpOAuth: config => request('/api/mcp-oauth/discover', { method: 'POST', ...json(config) }),
  startMcpOAuth: (name, config) => request(`/api/mcp-oauth/start/${encodeURIComponent(name)}`, { method: 'POST', ...json(config) }),
  pollMcpOAuth: id => request(`/api/mcp-oauth/login/${encodeURIComponent(id)}`),
  forwardMcpOAuth: (id, callback) => request(`/api/mcp-oauth/callback/${encodeURIComponent(id)}`, { method: 'POST', ...json(callback) }),
  cancelMcpOAuth: id => request(`/api/mcp-oauth/login/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  mcpServers: () => request('/api/mcp-servers'),
  saveMcpServer: (name, config = {}) =>
    request(`/api/mcp-servers/${encodeURIComponent(name)}`, { method: 'PUT', ...json(config) }),
  removeMcpServer: (name) =>
    request(`/api/mcp-servers/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  testMcpServer: (name) =>
    request(`/api/mcp-servers/${encodeURIComponent(name)}/test`, { method: 'POST' }),

  projects: () => request('/api/projects'),
  projectApp: () => request('/api/projects/github-app'),
  configureMentions: (id, payload) => request(`/api/projects/github-app/${encodeURIComponent(id)}/mentions`, { method: 'PUT', ...json(payload) }),
  connectProjectApp: (payload) => request('/api/projects/github-app/connect', { method: 'POST', ...json(payload) }),
  projectRepositories: (page = 1, connectionId = '') => request(`/api/projects/repositories?page=${page}&connection_id=${encodeURIComponent(connectionId)}`),
  addProject: (fullName, connectionId = '') => request('/api/projects', { method: 'POST', ...json({ full_name: fullName, connection_id: connectionId }) }),
  removeProject: (id) => request(`/api/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  chats: () => request('/api/chats'),
  searchChats: (query, signal) => request(`/api/search/chats?${new URLSearchParams({ q: query })}`, { signal }),
  createChat: (payload = {}) => request('/api/chats', { method: 'POST', ...json(account.user?.role === 'member' ? { ...(payload.title ? { title: payload.title } : {}), ...(payload.agent_id ? { agent_id: payload.agent_id } : {}) } : payload) }),
  chatDraft: id => request(`/api/chats/${id}/draft`, { signal: AbortSignal.timeout(15000) }),
  saveChatDraft: (id, payload) => request(`/api/chats/${id}/draft`, { method: 'PUT', ...json(payload), signal: AbortSignal.timeout(15000) }),
  chat: (id, signal = AbortSignal.timeout(15000)) => request(`/api/chats/${id}`, { signal }),
  projectChanges: (id, signal) => request(`/api/chats/${encodeURIComponent(id)}/project-changes`, { signal }),
  outputRevisions: (chatId, outputId, signal) => request(`/api/chats/${encodeURIComponent(chatId)}/outputs/${encodeURIComponent(outputId)}/revisions`, { signal }),
  outputFile: (chatId, outputId, revision, fileId, { preview = false, signal } = {}) => request(
    `/api/chats/${encodeURIComponent(chatId)}/outputs/${encodeURIComponent(outputId)}/revisions/${revision}/files/${encodeURIComponent(fileId)}${preview ? '?preview=true' : ''}`,
    { responseType: 'blob', signal }
  ),
  uploadAttachment: (id, file) => request(`/api/chats/${id}/attachments?name=${encodeURIComponent(file.name)}`, {
    method: 'POST', body: file, headers: { 'Content-Type': file.type || 'application/octet-stream' }, signal: AbortSignal.timeout(60000)
  }),
  attachment: (chatId, id, signal) => request(`/api/chats/${chatId}/attachments/${id}`, { responseType: 'blob', signal }),
  discardAttachment: (chatId, id) => request(`/api/chats/${chatId}/attachments/${id}`, { method: 'DELETE' }),
  sendMessage: (id, text, messageId, projectIds, attachments = []) => request(`/api/chats/${id}/messages`, {
    method: 'POST', headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(20000), ...json({ text, message_id: messageId, project_ids: projectIds,
      attachment_ids: attachments.map((image) => image.id), queue: true })
  }),
  stopChat: (id, turnId) => request(`/api/chats/${id}/stop`, { method: 'POST', ...json({ turn_id: turnId }) }),
  resumeChatQueue: (id) => request(`/api/chats/${id}/queue/resume`, { method: 'POST' }),
  discardQueuedMessage: (id, messageId) => request(`/api/chats/${id}/queue/${encodeURIComponent(messageId)}`, { method: 'DELETE' }),
  updateChat: (id, payload) => request(`/api/chats/${id}`, { method: 'PATCH', ...json(payload) }),
  regenerateChatTitle: (id) => request(`/api/chats/${id}/title/regenerate`, { method: 'POST' }),
  updateChatReadState: (id, payload) => request(`/api/chats/${id}/read-state`, { method: 'PATCH', ...json(payload) }),
  branchChat: (id) => request(`/api/chats/${id}/branch`, { method: 'POST' }),
  deleteChat: (id) => request(`/api/chats/${id}`, { method: 'DELETE' }),

  workflows: () => request('/api/workflows'),
  workflow: (name) => request(`/api/workflows/${name}`),
  workflowInputOptions: (name, field, params = {}, signal) => request(
    `/api/workflows/${encodeURIComponent(name)}/input-options?${new URLSearchParams({ field, ...params })}`, { signal }),
  workflowSettings: (name, payload) =>
    request(`/api/workflows/${name}/settings`, { method: 'PATCH', ...json(payload) }),
  deleteWorkflow: (name) => request(`/api/workflows/${name}`, { method: 'DELETE' }),
  runWorkflow: (name, input) => request(`/api/workflows/${name}/run`, { method: 'POST', ...json({ input }) }),
  createSchedule: (name, schedule) => request(`/api/workflows/${name}/schedules`, { method: 'POST', ...json(schedule) }),
  updateSchedule: (name, id, schedule) => request(`/api/workflows/${name}/schedules/${encodeURIComponent(id)}`, { method: 'PUT', ...json(schedule) }),
  deleteSchedule: (name, id) => request(`/api/workflows/${name}/schedules/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  validate: (name, code) => request('/api/workflows/validate', { method: 'POST', ...json({ name, code }) }),

  drafts: () => request('/api/drafts'),
  draft: (name) => request(`/api/drafts/${name}`),
  approveDraft: (name) => request(`/api/drafts/${name}/approve`, { method: 'POST' }),
  discardDraft: (name) => request(`/api/drafts/${name}`, { method: 'DELETE' }),

  runs: (workflow, params = {}, signal) => request(`/api/runs?${new URLSearchParams({ ...(workflow ? { workflow } : {}), ...params })}`, { signal }),
  run: (id) => request(`/api/runs/${id}`),
  runGraph: (id, signal) => request(`/api/runs/${encodeURIComponent(id)}/graph`, { signal }),
  cancelRun: (id) => request(`/api/runs/${id}/cancel`, { method: 'POST' }),
  terminateRun: (id) => request(`/api/runs/${id}/terminate`, { method: 'POST', ...json({}) })
}

function stream (path, onEvent, onError, onOpen) {
  // Capture the server and credentials for this connection. Replacing it aborts
  // outstanding probes, so old-instance failures cannot gate the new instance.
  const base = endpoint(path)
  return liveConnection(base, {
    probe: async signal => {
      let response = await fetch(base, { method: 'HEAD', signal, cache: 'no-store', credentials: isNative ? 'omit' : 'same-origin', headers: headers() })
      if (response.status === 401 && isNative && refreshSession) {
        await refreshSession()
        response = await fetch(base, { method: 'HEAD', signal, cache: 'no-store', credentials: 'omit', headers: headers() })
      }
      return response
    },
    onMessage: message => {
      let data
      try { data = JSON.parse(message.data) } catch { return }
      onEvent(data)
    },
    onError,
    onOpen
  }, isNative ? { EventSource: BearerEventSource } : {})
}

export function chatStream (chatId, onSnapshot, onError) {
  return stream(`/api/chats/${encodeURIComponent(chatId)}/stream`, data => {
    if (data.type === 'snapshot') onSnapshot(data)
  }, onError)
}

/** Live system events, supervised across outages, sleep and credential failures. */
export function liveEvents (onEvent, onError, onOpen) {
  return stream('/api/events', onEvent, onError, onOpen)
}
