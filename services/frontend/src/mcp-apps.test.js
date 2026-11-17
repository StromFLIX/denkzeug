import test from 'node:test'
import assert from 'node:assert/strict'
import { appHeight, appHostOrigin, appSurface, sandboxAddress, externalAppLink, AppFrameTransport } from './mcp-apps.js'

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

test('bundled desktop/iOS and Android host origins never become opaque null', () => {
  assert.equal(new URL('capacitor://localhost/chats/one').origin, 'null')
  assert.equal(appHostOrigin('capacitor://localhost/chats/one?private=value#result'), 'capacitor://localhost')
  assert.equal(appHostOrigin('https://localhost/chats/one'), 'https://localhost')
  assert.equal(appHostOrigin('https://app.example/chats/one'), 'https://app.example')
  for (const host of ['capacitor://localhost/chats/one', 'https://localhost/chats/one']) {
    const url = sandboxAddress('https://sandbox.example', host)
    assert.equal(url.searchParams.get('parent'), host.startsWith('capacitor:') ? 'capacitor://localhost' : 'https://localhost')
    assert.ok(!url.href.includes('private'))
  }
  for (const host of ['null', 'file:///tmp/app.html', 'data:text/html,hi', 'javascript:alert(1)', 'capacitor://evil', 'capacitor://localhost.evil', 'capacitor://localhost:443', 'capacitor://user@localhost', 'http://remote.example']) {
    assert.throws(() => appHostOrigin(host))
  }
  assert.throws(() => sandboxAddress('capacitor://localhost', 'https://app.example'))
  assert.throws(() => sandboxAddress('https://localhost', 'https://localhost/chats/one'))
})

test('frame surfaces use bounded theme colors, never arbitrary CSS', () => {
  assert.deepEqual(appSurface({ theme: 'dark', styles: { variables: {
    '--color-background-primary': '#171b26', '--color-text-primary': '#edf0f8'
  } } }), { theme: 'dark', background: '#171b26', color: '#edf0f8' })
  assert.deepEqual(appSurface(), { theme: 'light', background: '#ffffff', color: '#000000' })
  for (const value of ['transparent', '#fff', 'red; background:url(https://evil.example)', '</script><script>alert(1)</script>', {}, null]) {
    assert.deepEqual(appSurface({ theme: 'dark', styles: { variables: {
      '--color-background-primary': value, '--color-text-primary': value
    } } }), { theme: 'dark', background: '#181818', color: '#ffffff' })
  }
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
