import { request, server, setRefreshHandler } from './api.js'
import { account, auth, acceptAccount, clearAccount, isNative, secureSession } from './auth-session.js'

const send = (path, body = {}, method = 'POST') => request(`/api/auth/${path}`, { method, body: JSON.stringify(body) })
let refreshing = null
async function accept (data, origin = server.url) {
  const epoch = account.epoch
  if (!data?.user?.id || data.user.status !== 'active') throw new Error('This account is not active.')
  if (isNative && (!data.access_token || !data.refresh_token)) throw new Error('This server did not return a native account session. Update the server and app.')
  // Store the rotating refresh secret before exposing the session to application code.
  if (isNative && data.refresh_token) await secureSession('set', origin, data.refresh_token)
  if (epoch !== account.epoch || origin !== server.url) throw new Error('Account changed.')
  acceptAccount(data)
  return data
}
export async function refreshAccount () {
  if (refreshing) return refreshing
  const origin = server.url, epoch = account.epoch
  refreshing = (async () => {
    const token = await secureSession('get', origin)
    if (!token) throw new Error('Sign in to continue.')
    if (origin !== server.url || epoch !== account.epoch) throw new Error('Account changed.')
    const data = await send('refresh', { refresh_token: token })
    if (origin !== server.url || epoch !== account.epoch) throw new Error('Account changed.')
    if (!data.user && data.access_token) {
      auth.token = data.access_token
      // This is already inside refresh: a rejected new token must fail, not
      // recursively await the same in-flight refresh promise.
      const me = await request('/api/auth/me', {}, 1)
      Object.assign(data, me)
    }
    return accept(data, origin)
  })().catch(async error => {
    // Never let an old server's refresh failure close a newer account.
    if (origin === server.url && epoch === account.epoch) {
      clearAccount()
      if (error.status === 401) await secureSession('remove', origin).catch(() => {})
    }
    throw error
  }).finally(() => { refreshing = null })
  return refreshing
}
setRefreshHandler(refreshAccount)
export const accountApi = {
  meta: () => request('/api/auth/meta'),
  async initialize () {
    account.ready = false
    account.error = ''
    if (isNative && !server.url) { account.ready = true; return }
    try {
      if (isNative) await refreshAccount()
      else acceptAccount(await request('/api/auth/me'))
    } catch (error) {
      clearAccount()
      if (error.status !== 401 && error.message !== 'Sign in to continue.') account.error = error.message
    } finally { account.ready = true }
  },
  async login (username, password) { return accept(await send('login', { username, password, ...(isNative ? { native: true } : {}) })) },
  async bootstrap (body) {
    const data = await send('bootstrap', { ...body, ...(isNative ? { native: true } : {}) })
    if (isNative && !data.access_token) return accountApi.login(body.username, body.password)
    if (data.user) await accept(data)
    return data
  },
  invitation: code => send('invitation', { code }),
  async register (body) {
    const data = await send('register', { ...body, ...(isNative ? { native: true } : {}) })
    if (isNative && !data.verification_required && !data.access_token) return accountApi.login(body.username, body.password)
    if (data.user) await accept(data)
    return data
  },
  verify: token => send('verify-email', { token }),
  requestVerification: () => send('verify-email/request'),
  securityEvents: () => request('/api/auth/security-events'),
  recoveryCode: id => send(`users/${encodeURIComponent(id)}/recovery`),
  forgot: email => send('forgot-password', { email }),
  async reset (body) { await send('reset-password', body); clearAccount(); if (isNative) await secureSession('remove', server.url) },
  async changePassword (body) {
    await send('change-password', body)
    clearAccount()
    if (isNative) await secureSession('remove', server.url)
    try { localStorage.setItem('denkzeug.account.logout', String(Date.now())) } catch { /* optional broadcast */ }
  },
  async me () { const data = await request('/api/auth/me'); acceptAccount(data); return data },
  async profile (body) { await send('profile', body, 'PATCH'); await accountApi.me() },
  sessions: () => request('/api/auth/sessions'),
  revokeSession: id => request(`/api/auth/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  users: () => request('/api/auth/users'),
  updateUser: (id, body) => send(`users/${encodeURIComponent(id)}`, body, 'PATCH'),
  invitations: () => request('/api/auth/invitations'),
  invite: body => send('invitations', body),
  revokeInvitation: id => send(`invitations/${encodeURIComponent(id)}/revoke`),
  policy: () => request('/api/auth/policy'),
  resources: () => request('/api/access/resources'),
  grants: () => request('/api/access/grants'),
  share: body => request('/api/access/grants', { method: 'PUT', body: JSON.stringify(body) }),
  unshare: body => request('/api/access/grants', { method: 'DELETE', body: JSON.stringify(body) }),
  mailStatus: () => request('/api/auth/mail-status'),
  testEmail: () => send('test-email'),
  savePolicy: body => send('policy', body, 'PATCH'),
  reauth: password => send('reauth', { password }),
  async logout (allDevices = false) {
    // Do not report a successful logout while the server session remains active.
    await send('logout', { all_devices: allDevices })
    clearAccount()
    if (isNative) await secureSession('remove', server.url)
    // Other tabs must close private views without receiving any credentials.
    try { localStorage.setItem('denkzeug.account.logout', String(Date.now())) } catch { /* optional cross-tab broadcast */ }
  },
  async switchServer (url) {
    if (account.user) await accountApi.logout()
    clearAccount()
    server.url = url
    await accountApi.initialize()
  }
}
if (globalThis.addEventListener) globalThis.addEventListener('storage', event => {
  if (event.key === 'denkzeug.account.logout') clearAccount()
})

export const memberSettingKeys = new Set(['account', 'api-keys', 'appearance', 'workspace', 'usage', 'apps'])
export const canOpenRoute = route => account.user?.role === 'admin' || ['chats', 'workflows', 'workflow-run', 'runs'].includes(route.name) || (route.name === 'settings' && memberSettingKeys.has(route.params.tab))
