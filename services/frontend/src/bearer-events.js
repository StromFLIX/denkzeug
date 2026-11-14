import { auth } from './auth-session.js'

/** EventSource-compatible transport for native bearer auth. Tokens never enter URLs. */
export class BearerEventSource {
  constructor (url) {
    this.readyState = 0
    this.listeners = new Map()
    this.controller = new AbortController()
    queueMicrotask(() => this.open(url))
  }
  addEventListener (name, listener) { this.listeners.set(name, listener) }
  close () { this.readyState = 2; this.controller.abort() }
  async open (url) {
    try {
      const response = await fetch(url, { headers: { Authorization: `Bearer ${auth.token}`, Accept: 'text/event-stream' }, credentials: 'omit', signal: this.controller.signal })
      if (!response.ok || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('Stream rejected')
      if (this.readyState === 2) return
      this.readyState = 1
      this.onopen?.()
      const reader = response.body.getReader(), decoder = new TextDecoder()
      let pending = '', event = '', data = []
      while (this.readyState !== 2) {
        const chunk = await reader.read()
        if (chunk.done) throw new Error('Stream ended')
        pending += decoder.decode(chunk.value, { stream: true })
        let end
        while ((end = pending.indexOf('\n')) >= 0) {
          const line = pending.slice(0, end).replace(/\r$/, '')
          pending = pending.slice(end + 1)
          if (!line) {
            if (data.length) {
              const message = { data: data.join('\n') }
              if (!event || event === 'message') this.onmessage?.(message)
              else this.listeners.get(event)?.(message)
            }
            event = ''; data = []
          } else if (line.startsWith('event:')) event = line.slice(6).trimStart()
          else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
        }
      }
    } catch (error) { if (this.readyState !== 2) this.onerror?.(error) }
  }
}
