import test from 'node:test'
import assert from 'node:assert/strict'
import { appHeight, sandboxAddress, externalAppLink, AppFrameTransport } from './mcp-apps.js'

test('sandbox requires an isolated, credential-free secure origin', () => {
  const url = sandboxAddress('https://sandbox.example', 'https://app.example', { resourceDomains: ['https://cdn.example'] })
  assert.equal(url.pathname, '/sandbox.html')
  assert.equal(url.searchParams.get('parent'), 'https://app.example')
  assert.deepEqual(JSON.parse(url.searchParams.get('csp')), { resourceDomains: ['https://cdn.example'] })
  for (const bad of ['https://app.example', 'http://remote.example', 'https://user:pass@sandbox.example', 'javascript:alert(1)', 'https://sandbox.example?parent=other']) {
    assert.throws(() => sandboxAddress(bad, 'https://app.example'))
  }
  assert.equal(sandboxAddress('http://localhost:8092', 'http://localhost:9000').origin, 'http://localhost:8092')
})

test('layout and external links are bounded', () => {
  assert.equal(appHeight(50000), 1400)
  assert.equal(appHeight(-1), 100)
  assert.equal(appHeight(NaN), 320)
  assert.equal(externalAppLink('https://github.com/org/repo'), 'https://github.com/org/repo')
  for (const bad of ['javascript:alert(1)', 'data:text/html,test', 'file:///etc/passwd', 'https://user:pass@example.com', 'http://example.com']) assert.throws(() => externalAppLink(bad))
})

test('transport binds messages to the exact proxy window and origin', async () => {
  const listeners = new Set(), sent = [], received = []
  globalThis.window = { addEventListener: (_, fn) => listeners.add(fn), removeEventListener: (_, fn) => listeners.delete(fn) }
  const source = { postMessage: (...args) => sent.push(args) }
  const transport = new AppFrameTransport({ contentWindow: source }, 'https://sandbox.example')
  transport.onmessage = event => received.push(event)
  await transport.start()
  const listener = [...listeners][0], data = { jsonrpc: '2.0', id: 1, result: {} }
  listener({ source: {}, origin: 'https://sandbox.example', data })
  listener({ source, origin: 'https://evil.example', data })
  listener({ source, origin: 'https://sandbox.example', data: { hello: true } })
  listener({ source, origin: 'https://sandbox.example', data })
  assert.deepEqual(received, [data])
  await transport.send(data)
  assert.deepEqual(sent, [[data, 'https://sandbox.example']])
  await transport.close()
  assert.equal(listeners.size, 0)
  delete globalThis.window
})
