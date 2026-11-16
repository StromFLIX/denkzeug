// Credential-free, host-controlled MCP Apps sandbox proxy. No application APIs.
const params = new URLSearchParams(location.search)
const hostOrigin = params.get('parent')
const view = document.createElement('iframe')
view.title = 'App content'
view.setAttribute('sandbox', 'allow-scripts')
view.setAttribute('referrerpolicy', 'no-referrer')
view.style.cssText = 'display:block;border:0;width:100%;height:100vh;background:transparent'
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
    if (message.method === 'ui/notifications/sandbox-resource-ready') {
      if (loaded || typeof message.params?.html !== 'string' || message.params.html.length > 1100000) return
      loaded = true
      // The response header's CSP is inherited by srcdoc and cannot be loosened
      // by the app. Opaque inner origin also prevents access to the proxy DOM.
      // Defense in depth for browser APIs not governed by connect-src. The
      // opaque app cannot borrow constructors from this proxy; deny nested
      // browsing/worker realms before any authored script executes.
      const guard = `<meta http-equiv="Content-Security-Policy" content="frame-src 'none'; child-src 'none'; worker-src 'none'"><meta http-equiv="x-dns-prefetch-control" content="off"><script>for(const key of ['RTCPeerConnection','webkitRTCPeerConnection']){Object.defineProperty(globalThis,key,{value:undefined,writable:false,configurable:false})}<\/script>`
      view.srcdoc = '<!doctype html>' + guard + message.params.html
    } else if (loaded && !message.method?.startsWith('ui/notifications/sandbox-')) {
      view.contentWindow.postMessage(message, '*')
    }
  } else if (loaded && event.source === view.contentWindow && event.origin === 'null') {
    if (!message.method?.startsWith('ui/notifications/sandbox-')) parent.postMessage(message, hostOrigin)
  }
})
parent.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/sandbox-proxy-ready', params: {} }, hostOrigin)
