import { brand } from './brand.js'

export function desktopOAuth () {
  const bridge = (globalThis.denkzeugDesktop || globalThis.nautionetteDesktop)?.oauth
  return bridge?.version === 1 && ['prepare', 'authorize', 'cancel'].every(key => typeof bridge[key] === 'function') ? bridge : null
}

export const oauthClientMessage = `This OAuth connection requires an updated ${brand.name} Windows, macOS or Linux desktop app. Web clients support hosted web callbacks, device codes and client credentials instead.`

export function requiresDesktopOAuth (config = {}) {
  const hosted = config.transport === 'http' || config.mode === 'https'
  return !hosted || ((config.grant_type || 'authorization_code') === 'authorization_code' && (config.callback_mode || 'desktop') === 'desktop')
}

export function oauthSupported (config, bridge = desktopOAuth()) {
  return !requiresDesktopOAuth(config) || Boolean(bridge)
}

function openOAuthBrowser (value) {
  const url = new URL(value)
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Provider sign-in URL must use HTTPS without credentials')
  globalThis.open(url.href, '_blank', 'noopener,noreferrer')
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms))

// No credentials/callbacks in reactive state, localStorage, chat or URL params.
// Each new CLI authorization URL gets its own exact port and one-shot receiver.
export function oauthConnection ({ bridge = desktopOAuth(), backend, interval = 1000, context, openBrowser = openOAuthBrowser } = {}) {
  if (!backend || typeof context !== 'function') throw new Error('OAuth needs a backend adapter and instance context')
  const initialContext = context()
  let cancelled = false, loginId = '', usesDesktop = false
  function check () {
    if (cancelled) throw new Error('OAuth cancelled')
    if (context() !== initialContext) throw new Error('Instance or credentials changed; start a new login')
  }
  async function cancel () {
    cancelled = true
    if (usesDesktop) await bridge?.cancel().catch(() => {})
    // Never send a callback (or even a cancellation) to a newly selected instance.
    if (loginId && context() === initialContext) await backend.cancelMcpOAuth(loginId).catch(() => {})
  }
  async function receive (authorization, prepared = null) {
    const receiver = prepared || await bridge.prepare(authorization.redirect_uri)
    if (prepared && prepared.redirect_uri !== authorization.redirect_uri) throw new Error('OAuth callback changed after preparation; start again')
    check()
    let finished = false
    const callback = bridge.authorize({ ...authorization, id: receiver.id })
    const watch = (async () => {
      while (!finished) {
        await delay(interval)
        if (finished) return
        check()
        const result = await backend.pollMcpOAuth(loginId)
        if (finished) return
        if (['failed', 'expired', 'cancelled'].includes(result.status)) throw new Error(result.message || 'CLI login stopped')
      }
    })()
    try { return await Promise.race([callback, watch]) } finally { finished = true }
  }
  async function connect (name, config, onStage = () => {}) {
    const desktop = requiresDesktopOAuth(config)
    if (desktop && !bridge) throw new Error(oauthClientMessage)
    usesDesktop = desktop
    const seen = new Set()
    const hosted = config.transport === 'http' || config.mode === 'https'
    const grant = config.grant_type || 'authorization_code'
    let prepared = null, deviceOpened = false, browserStage = '', pollInterval = interval
    try {
      check()
      let payload = { ...config }
      if (desktop) payload.desktop_bridge_version = bridge.version
      if (hosted && desktop) {
        onStage('Opening the local OAuth callback port…')
        prepared = await bridge.prepare(config.redirect_uri)
        check()
        payload = { ...payload, redirect_uri: prepared.redirect_uri, redirect_uri_template: config.redirect_uri }
      }
      onStage(hosted ? 'Starting hosted HTTPS OAuth…' : 'Starting CLI login inside agentgateway…')
      const login = await backend.startMcpOAuth(name, payload)
      loginId = login.id
      check()
      while (true) {
        check()
        const result = await backend.pollMcpOAuth(loginId)
        check()
        if (result.status === 'complete') { loginId = ''; return result }
        if (['failed', 'expired', 'cancelled'].includes(result.status)) throw new Error(result.message || 'CLI login failed')
        const device = result.device
        if (device && hosted && grant === 'device_code') {
          browserStage = `Enter code ${device.user_code} at ${device.verification_uri}. Waiting for device sign-in…`
          if (Number.isFinite(device.interval) && device.interval > 0) pollInterval = Math.max(interval, device.interval * 1000)
          onStage(browserStage)
          if (!deviceOpened) {
            deviceOpened = true
            check()
            await openBrowser(device.verification_uri_complete || device.verification_uri)
            check()
          }
        } else onStage(browserStage || result.message || (hosted ? 'Waiting for authentication…' : 'Waiting for CLI…'))
        const authorization = result.authorization
        if (authorization && grant !== 'client_credentials' && !seen.has(authorization.id)) {
          seen.add(authorization.id)
          if (seen.size > 8) throw new Error('Too many CLI login attempts; start again')
          if (hosted && !desktop) {
            if (grant !== 'authorization_code' || authorization.callback_mode !== 'web') throw new Error('Unexpected OAuth callback mode; start again')
            browserStage = 'Complete sign-in in your browser. Waiting for the web callback…'
            onStage(browserStage)
            check()
            await openBrowser(authorization.authorization_url)
            check()
            await delay(pollInterval)
            continue
          }
          if (hosted && authorization.callback_mode === 'web') throw new Error('OAuth callback mode changed; start again')
          onStage(`Sign in in your browser · detected local port ${authorization.port} · ${authorization.response_mode}`)
          const callback = await receive(authorization, prepared)
          prepared = null
          check()
          onStage(hosted ? 'Exchanging the code in the backend; checking HTTPS MCP…' : 'Forwarding callback to the CLI; checking authentication and MCP…')
          // No automatic mutation retry. The gateway fences the capture before
          // contacting its CLI. A lost response is not permission to replay code.
          const forwarded = await backend.forwardMcpOAuth(loginId, { capture_id: authorization.id, ...callback })
          if (forwarded.status !== 'forwarded') throw new Error('Callback delivery is uncertain; start a fresh login. It will not be replayed.')
          if (callback.fields.error) throw new Error('Provider refused or cancelled sign-in')
        }
        await delay(pollInterval)
      }
    } catch (error) {
      await cancel()
      throw error
    } finally { if (desktop) await bridge?.cancel().catch(() => {}) }
  }
  return { connect, cancel }
}
