// Credential-free, host-controlled MCP Apps sandbox proxy. No application APIs.
const params = new URLSearchParams(location.search)
const hostOrigin = params.get('parent')
const view = document.createElement('iframe')
view.title = 'App content'
view.setAttribute('sandbox', 'allow-scripts')
view.setAttribute('referrerpolicy', 'no-referrer')
view.style.cssText = 'display:block;border:0;width:100%;height:100vh'

// This function also runs before authored HTML in the opaque inner document.
// Defaults are an early stylesheet, not inline/!important overrides: an app's
// own explicit surfaces still win. Only bounded hex colors enter CSS/source.
function installSurface (initial, frame) {
  const hex = (value, fallback) => typeof value === 'string' && /^#[\da-f]{6}$/i.test(value) ? value : fallback
  let theme = initial.theme === 'dark' ? 'dark' : 'light'
  let background = hex(initial.background, theme === 'dark' ? '#181818' : '#ffffff')
  let color = hex(initial.color, theme === 'dark' ? '#ffffff' : '#000000')
  const style = document.createElement('style')
  document.head.append(style)
  const paint = () => {
    style.textContent = `html { color-scheme: ${theme}; background-color: ${background}; color: ${color}; }`
    if (frame) {
      frame.style.colorScheme = theme
      frame.style.backgroundColor = background
    }
  }
  paint()
  return {
    current: () => ({ theme, background, color }),
    update: context => {
      if (!context || typeof context !== 'object') return
      if (context.theme === 'dark' || context.theme === 'light') {
        if (context.theme !== theme) {
          background = context.theme === 'dark' ? '#181818' : '#ffffff'
          color = context.theme === 'dark' ? '#ffffff' : '#000000'
        }
        theme = context.theme
      }
      const variables = context.styles?.variables || {}
      background = hex(variables['--color-background-primary'], background)
      color = hex(variables['--color-text-primary'], color)
      paint()
    }
  }
}
const surface = installSurface(Object.fromEntries(['theme', 'background', 'color'].map(key => [key, params.get(key)])), view)
document.body.append(view)
let loaded = false
let windowStart = Date.now(), messages = 0
const valid = data => {
  if (!data || typeof data !== 'object' || data.jsonrpc !== '2.0') return false
  try { return JSON.stringify(data).length <= 2200000 } catch { return false }
}
window.addEventListener('message', event => {
  if (!valid(event.data)) return
  if (Date.now() - windowStart > 10000) { windowStart = Date.now(); messages = 0 }
  if (++messages > 400) return
  const message = event.data
  if (event.source === parent && event.origin === hostOrigin) {
    if (message.method === 'ui/notifications/host-context-changed') surface.update(message.params)
    if (message.method === 'ui/notifications/sandbox-resource-ready') {
      if (loaded || typeof message.params?.html !== 'string' || message.params.html.length > 1100000) return
      loaded = true
      // The response header's CSP is inherited by srcdoc and cannot be loosened
      // by the app. Opaque inner origin also prevents access to the proxy DOM.
      // Defense in depth for browser APIs not governed by connect-src. The
      // opaque app cannot borrow constructors from this proxy; deny nested
      // browsing/worker realms before any authored script executes.
      const guard = `<meta http-equiv="Content-Security-Policy" content="frame-src 'none'; child-src 'none'; worker-src 'none'"><meta http-equiv="x-dns-prefetch-control" content="off"><script>for(const key of ['RTCPeerConnection','webkitRTCPeerConnection']){Object.defineProperty(globalThis,key,{value:undefined,writable:false,configurable:false})}<\/script>`
      const paint = `<script>(()=>{const surface=(${installSurface.toString()})(${JSON.stringify(surface.current())});window.addEventListener('message',event=>{if(event.source===parent&&event.origin===${JSON.stringify(location.origin)}&&event.data?.jsonrpc==='2.0'&&event.data.method==='ui/notifications/host-context-changed')surface.update(event.data.params)})})()<\/script>`
      view.srcdoc = '<!doctype html>' + guard + paint + message.params.html
    } else if (loaded && !message.method?.startsWith('ui/notifications/sandbox-')) {
      view.contentWindow.postMessage(message, '*')
    }
  } else if (loaded && event.source === view.contentWindow && event.origin === 'null') {
    if (!message.method?.startsWith('ui/notifications/sandbox-')) parent.postMessage(message, hostOrigin)
  }
})
parent.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/sandbox-proxy-ready', params: {} }, hostOrigin)
