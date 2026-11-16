import { AppBridge } from '@modelcontextprotocol/ext-apps/app-bridge'

export const MAX_APP_HEIGHT = 1400
export function appHeight (height) {
  return Number.isFinite(height) ? Math.min(MAX_APP_HEIGHT, Math.max(100, Math.ceil(height))) : 320
}

export function sandboxAddress (configured, parent, csp = {}) {
  const url = new URL(configured)
  const host = new URL(parent)
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) || url.username || url.password || url.origin === host.origin || url.search || url.hash) {
    throw new Error('Apps require a separate HTTPS sandbox origin (HTTP is allowed on localhost).')
  }
  url.pathname = '/sandbox.html'
  url.searchParams.set('parent', host.origin)
  url.searchParams.set('csp', JSON.stringify(csp))
  return url
}

export function externalAppLink (value) {
  const url = new URL(value)
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Apps may only open HTTPS links.')
  return url.href
}

/** SDK transport with exact window AND origin binding; never send to "*". */
export class AppFrameTransport {
  constructor (frame, origin) { this.frame = frame; this.origin = origin }
  async start () {
    let windowStart = Date.now(), count = 0
    this.listener = event => {
      if (event.source !== this.frame.contentWindow || event.origin !== this.origin) return
      if (!event.data || event.data.jsonrpc !== '2.0') return
      if (Date.now() - windowStart > 10000) { windowStart = Date.now(); count = 0 }
      if (++count > 400) return
      try { if (JSON.stringify(event.data).length > 2200000) return } catch { return }
      this.onmessage?.(event.data)
    }
    window.addEventListener('message', this.listener)
  }
  async send (message) { this.frame.contentWindow?.postMessage(message, this.origin) }
  async close () { window.removeEventListener('message', this.listener); this.onclose?.() }
}

export async function connectApp (frame, app, sandbox, { context, resize, ready, error, openLink }) {
  const address = sandboxAddress(sandbox, window.location.origin, app.csp || {})
  const bridge = new AppBridge(null, { name: 'Denkzeug', version: '1' }, { openLinks: {} }, { hostContext: context })
  let closed = false, delivered = false, resourcesSent = false
  const fail = () => { if (!closed) error('The app could not initialize. Your original result is still available.') }
  const timer = setTimeout(fail, 15000)
  bridge.onsandboxready = async () => {
    if (resourcesSent || closed) return
    resourcesSent = true
    try { await bridge.sendSandboxResourceReady({ html: app.html, sandbox: 'allow-scripts', csp: app.csp || {} }) } catch { fail() }
  }
  bridge.oninitialized = async () => {
    if (delivered || closed) return
    delivered = true
    try {
      await bridge.sendToolInput({ arguments: app.input || {} })
      await bridge.sendToolResult(app.result || { content: [] })
      clearTimeout(timer)
      ready()
    } catch { fail() }
  }
  bridge.onsizechange = ({ height }) => resize(appHeight(height))
  bridge.onopenlink = async ({ url }) => {
    openLink(externalAppLink(url))
    return {}
  }
  // No automatic MCP client forwarding: this release grants no tools, chat
  // messaging, model-context updates, storage or backend mutations to a view.
  const transport = new AppFrameTransport(frame, address.origin)
  transport.onerror = fail
  await bridge.connect(transport)
  frame.src = address.href
  return {
    setContext: value => { if (!closed && delivered) bridge.setHostContext(value) },
    async close () {
      if (closed) return
      closed = true
      clearTimeout(timer)
      try {
        if (delivered) await bridge.teardownResource({}, { timeout: 500 })
      } catch { /* Teardown is best effort, never rerun the original producer. */ }
      finally { await bridge.close() }
    }
  }
}
