import { reactive } from 'vue'

export const isNative = Boolean(globalThis.denkzeugDesktop || globalThis.nautionetteDesktop || globalThis.Capacitor?.isNativePlatform?.())
export const account = reactive({ ready: false, user: null, csrf: '', sessionId: '', epoch: 0, error: '' })
let accessToken = ''
// Never migrate the old instance-wide bearer credential into an account session.
try { globalThis.localStorage?.removeItem('nautionette.token') } catch { /* storage may be disabled */ }

export const auth = {
  get token () { return accessToken },
  set token (value) { accessToken = value || '' },
  get scope () { return account.user?.id || 'signed-out' }
}
export function clearAccount () {
  accessToken = ''
  account.epoch++
  account.user = null
  account.csrf = account.sessionId = ''
}
export function acceptAccount (data) {
  if (!data?.user?.id || data.user.status !== 'active') throw new Error('This account is not active.')
  if (account.user?.id !== data.user.id || account.user?.role !== data.user.role) account.epoch++
  account.user = data.user
  account.csrf = data.csrf_token || ''
  account.sessionId = data.session_id || ''
  if (data.access_token) accessToken = data.access_token
}

let vaultWrites = Promise.resolve()
/** Serialize vault mutations so an older asynchronous write cannot replace a newer login. */
export async function secureSession (operation, origin, value) {
  if (operation === 'get') { await vaultWrites; return nativeSession(operation, origin, value) }
  const pending = vaultWrites.then(() => nativeSession(operation, origin, value))
  vaultWrites = pending.catch(() => {})
  return pending
}

/** One narrow native bridge; no plaintext fallback (including on old clients). */
async function nativeSession (operation, origin, value) {
  const desktop = globalThis.denkzeugDesktop || globalThis.nautionetteDesktop
  let bridge = desktop?.accountStore
  if (!bridge && globalThis.Capacitor?.isNativePlatform?.()) {
    const { registerPlugin } = await import('@capacitor/core')
    bridge = registerPlugin('DenkzeugAccountStore')
  }
  if (!bridge) throw new Error('Secure account storage is unavailable. Update the app or unlock the OS credential store.')
  const result = await bridge[operation]({ origin, capability: globalThis.denkzeugAccountCapability, ...(value === undefined ? {} : { value }) })
  return result?.value || ''
}

export function consumeAuthFragment (location = globalThis.location, history = globalThis.history) {
  const params = new URLSearchParams(location.hash.slice(1))
  const aliases = { 'reset-password': 'reset', 'verify-email': 'verify' }
  const keys = ['bootstrap', 'invite', 'reset', 'verify', ...Object.keys(aliases)]
  const flow = keys.find(key => params.has(key))
  if (!flow) return null
  const token = params.get(flow)
  for (const key of keys) params.delete(key)
  const rest = params.toString()
  history.replaceState(history.state, '', `${location.pathname}${location.search}${rest ? `#${rest}` : ''}`)
  return { flow: aliases[flow] || flow, token }
}

// Capture and strip before Vue Router redirects or seeds native back history.
export const initialAuthFragment = globalThis.location && globalThis.history ? consumeAuthFragment() : null
