import { test } from 'node:test'
import assert from 'node:assert/strict'
import { desktopOAuth, oauthConnection, oauthSupported, requiresDesktopOAuth } from './mcp-oauth.js'
const authorization = (id = 'capture-1', port = 40001) => ({ id, port, redirect_uri: `http://localhost:${port}`,
  authorization_url: 'https://login.example.test/authorize', state: 'state', response_mode: 'form_post' })

function fixture () {
  const calls = [], prepared = []
  let forwarded = 0
  const backend = {
    startMcpOAuth: async (name, config) => { calls.push(['start', name, config]); return { id: 'backend-login' } },
    pollMcpOAuth: async () => forwarded ? { status: 'complete' } : { status: 'authorizing', authorization: authorization() },
    forwardMcpOAuth: async (id, callback) => { forwarded++; calls.push(['forward', id, callback]); return { status: 'forwarded' } },
    cancelMcpOAuth: async id => { calls.push(['cancel', id]) }
  }
  const bridge = { version: 1,
    prepare: async redirect => { prepared.push(redirect); return { id: 'receiver-id', port: 40001, redirect_uri: redirect.replace(':0', ':40001') } },
    authorize: async payload => { assert.equal(payload.id, 'receiver-id'); return { method: 'POST', fields: { state: 'state', code: 'private-code' } } },
    cancel: async () => {}
  }
  return { backend, bridge, calls, prepared }
}

test('web, mobile and old markers are not desktop OAuth capabilities', () => {
  for (const marker of [undefined, true, { oauth: { version: 0 } }]) {
    globalThis.nautionetteDesktop = marker
    assert.equal(desktopOAuth(), null)
  }
  delete globalThis.nautionetteDesktop
})

test('canonical desktop bridge is preferred while older installed clients remain supported', () => {
  const canonical = fixture().bridge, legacy = fixture().bridge
  try {
    globalThis.denkzeugDesktop = { oauth: canonical }
    globalThis.nautionetteDesktop = { oauth: legacy }
    assert.equal(desktopOAuth(), canonical)
    delete globalThis.denkzeugDesktop
    assert.equal(desktopOAuth(), legacy)
  } finally {
    delete globalThis.denkzeugDesktop
    delete globalThis.nautionetteDesktop
  }
})

test('CLI-owned login uses detected port and preserves POST; receiver ID never overwrites capture ID', async () => {
  const f = fixture()
  const controller = oauthConnection({ ...f, interval: 1, context: () => 'instance' })
  await controller.connect('example', { command: 'cli' })
  assert.deepEqual(f.prepared, ['http://localhost:40001'])
  assert.deepEqual(f.calls.find(c => c[0] === 'forward'), ['forward', 'backend-login', {
    capture_id: 'capture-1', method: 'POST', fields: { state: 'state', code: 'private-code' }
  }])
  assert.equal(f.calls[0][2].desktop_bridge_version, 1)
})

test('hosted OAuth prepares once before the backend mints PKCE and uses the exact redirect', async () => {
  const f = fixture()
  f.backend.startMcpOAuth = async (_name, config) => {
    assert.deepEqual(f.prepared, ['http://localhost:0'])
    assert.equal(config.redirect_uri, 'http://localhost:40001')
    assert.equal(config.redirect_uri_template, 'http://localhost:0')
    return { id: 'backend-login' }
  }
  await oauthConnection({ ...f, interval: 1, context: () => 'instance' }).connect('example', { transport: 'http', redirect_uri: 'http://localhost:0' })
  assert.equal(f.prepared.length, 1)
})

test('another CLI authorization opens its new port without replaying the old callback', async () => {
  const f = fixture()
  let count = 0
  f.backend.pollMcpOAuth = async () => count >= 2 ? { status: 'complete' } : {
    status: 'authorizing', authorization: authorization(`capture-${count}`, 40001 + count)
  }
  f.backend.forwardMcpOAuth = async (_id, callback) => { f.calls.push(callback); count++; return { status: 'forwarded' } }
  await oauthConnection({ ...f, interval: 1, context: () => 'instance' }).connect('example', {})
  assert.deepEqual(f.prepared, ['http://localhost:40001', 'http://localhost:40002'])
  assert.deepEqual(f.calls.filter(c => c.capture_id).map(c => c.capture_id), ['capture-0', 'capture-1'])
})

test('lost callback response is never automatically replayed', async () => {
  const f = fixture()
  let attempts = 0
  f.backend.forwardMcpOAuth = async () => { attempts++; throw new Error('Lost callback result; start again') }
  await assert.rejects(oauthConnection({ ...f, interval: 1, context: () => 'instance' }).connect('example', {}), /Lost callback/)
  assert.equal(attempts, 1)
  assert.ok(f.calls.some(c => c[0] === 'cancel'))
})

test('cancel during a slow start still cancels the late-created gateway job', async () => {
  const f = fixture()
  let resolve
  f.backend.startMcpOAuth = () => new Promise(r => { resolve = r })
  const controller = oauthConnection({ ...f, interval: 1, context: () => 'instance' })
  const running = controller.connect('example', {})
  const rejected = assert.rejects(running, /cancelled/)
  await controller.cancel()
  resolve({ id: 'late-login' })
  await rejected
  assert.ok(f.calls.some(c => c[0] === 'cancel' && c[1] === 'late-login'))
  assert.equal(f.prepared.length, 0)
})

test('switching instances after callback capture cannot send the code to another backend', async () => {
  const f = fixture()
  let context = 'old'
  f.bridge.authorize = async () => { context = 'new'; return { method: 'POST', fields: { code: 'private', state: 'state' } } }
  await assert.rejects(oauthConnection({ ...f, interval: 1, context: () => context }).connect('example', {}), /Instance or credentials changed/)
  assert.ok(!f.calls.some(c => c[0] === 'forward' || c[0] === 'cancel'))
})

test('no desktop bridge cannot start the CLI', async () => {
  const f = fixture()
  await assert.rejects(oauthConnection({ ...f, bridge: null, context: () => 'instance' }).connect('example', {}), /desktop app/)
  assert.equal(f.calls.length, 0)
})

test('hosted grant support is independent of desktop markers; legacy CLI stays desktop-only', () => {
  for (const config of [
    { transport: 'http', callback_mode: 'web' },
    { mode: 'https', grant_type: 'device_code' },
    { transport: 'http', grant_type: 'client_credentials' },
  ]) {
    assert.equal(requiresDesktopOAuth(config), false)
    assert.equal(oauthSupported(config, null), true)
  }
  for (const config of [{}, { mode: 'cli', grant_type: 'device_code' }, { transport: 'http' }]) {
    assert.equal(oauthSupported(config, null), false)
  }
})

function hostedFixture (result) {
  const f = fixture(), opened = [], stages = []
  let polls = 0
  f.backend.pollMcpOAuth = async () => ++polls > 2 ? { status: 'complete' } : result
  f.bridge.prepare = async () => { throw new Error('Must not prepare a local receiver') }
  f.bridge.authorize = async () => { throw new Error('Must not capture callbacks') }
  return { ...f, opened, stages, openBrowser: async url => { opened.push(url) } }
}

test('hosted web callback opens once and polls without bridge or callback forwarding', async () => {
  const f = hostedFixture({ status: 'authorizing', authorization: { id: 'web-capture', callback_mode: 'web', authorization_url: 'https://login.example.test/authorize' } })
  await oauthConnection({ ...f, bridge: null, interval: 1, context: () => 'instance' }).connect('web', { transport: 'http', callback_mode: 'web' }, value => f.stages.push(value))
  assert.deepEqual(f.opened, ['https://login.example.test/authorize'])
  assert.ok(f.stages.some(value => value.includes('web callback')))
  assert.equal('desktop_bridge_version' in f.calls[0][2], false)
  assert.equal(f.calls.some(c => c[0] === 'forward'), false)
})

test('device code displays the code, opens the complete verification URL once and never forwards', async () => {
  const device = { verification_uri: 'https://login.example.test/device', verification_uri_complete: 'https://login.example.test/device?code=ABCD', user_code: 'ABCD-EFGH', expires_in: 600, interval: 0.001 }
  const f = hostedFixture({ status: 'authorizing', device })
  await oauthConnection({ ...f, bridge: null, interval: 1, context: () => 'instance' }).connect('device', { transport: 'http', grant_type: 'device_code' }, value => f.stages.push(value))
  assert.deepEqual(f.opened, [device.verification_uri_complete])
  assert.ok(f.stages.some(value => value.includes('ABCD-EFGH') && value.includes(device.verification_uri)))
  assert.equal(f.calls.some(c => c[0] === 'forward'), false)
  assert.equal('desktop_bridge_version' in f.calls[0][2], false)
})

test('device verification URI works when there is no complete URL', async () => {
  const f = hostedFixture({ status: 'authorizing', device: { verification_uri: 'https://login.example.test/device', user_code: '1234', interval: 0.001 } })
  await oauthConnection({ ...f, interval: 1, context: () => 'instance' }).connect('device', { transport: 'http', grant_type: 'device_code' })
  assert.deepEqual(f.opened, ['https://login.example.test/device'])
})

test('client credentials polls without a browser, even if an authorization is returned', async () => {
  const f = hostedFixture({ status: 'authorizing', authorization: authorization() })
  await oauthConnection({ ...f, bridge: null, interval: 1, context: () => 'instance' }).connect('service', { transport: 'http', grant_type: 'client_credentials' })
  assert.deepEqual(f.opened, [])
  assert.equal(f.calls.some(c => c[0] === 'forward'), false)
})

test('non-desktop hosted failure cancels the backend job', async () => {
  const f = hostedFixture({ status: 'expired', message: 'Device code expired' })
  await assert.rejects(oauthConnection({ ...f, bridge: null, interval: 1, context: () => 'instance' }).connect('device', { transport: 'http', grant_type: 'device_code' }), /Device code expired/)
  assert.ok(f.calls.some(c => c[0] === 'cancel'))
})

test('web callback mode mismatch does not capture or forward a callback', async () => {
  const f = hostedFixture({ status: 'authorizing', authorization: authorization() })
  await assert.rejects(oauthConnection({ ...f, bridge: null, interval: 1, context: () => 'instance' }).connect('web', { transport: 'http', callback_mode: 'web' }), /Unexpected OAuth callback mode/)
  assert.deepEqual(f.opened, [])
  assert.equal(f.calls.some(c => c[0] === 'forward'), false)
})

test('instance changes while opening a device browser stop polling without cancelling the new instance', async () => {
  const f = hostedFixture({ status: 'authorizing', device: { verification_uri: 'https://login.example.test/device', user_code: '1234' } })
  let context = 'old'
  f.openBrowser = async () => { context = 'new' }
  await assert.rejects(oauthConnection({ ...f, bridge: null, interval: 1, context: () => context }).connect('device', { transport: 'http', grant_type: 'device_code' }), /Instance or credentials changed/)
  assert.equal(f.calls.some(c => c[0] === 'cancel'), false)
})

test('cancel during a non-desktop hosted slow start cancels the late-created job', async () => {
  const f = fixture()
  let resolve
  f.backend.startMcpOAuth = () => new Promise(r => { resolve = r })
  const controller = oauthConnection({ ...f, bridge: null, interval: 1, context: () => 'instance' })
  const running = controller.connect('web', { transport: 'http', callback_mode: 'web' })
  const rejected = assert.rejects(running, /cancelled/)
  await controller.cancel()
  resolve({ id: 'late-web-login' })
  await rejected
  assert.ok(f.calls.some(c => c[0] === 'cancel' && c[1] === 'late-web-login'))
})

test('device polling respects the provider interval and does not cancel an unused desktop bridge', async () => {
  const f = hostedFixture({ status: 'authorizing', device: { verification_uri: 'https://login.example.test/device', user_code: '1234', interval: 0.02 } })
  let cancels = 0, lastPoll = 0, count = 0
  f.bridge.cancel = async () => { cancels++ }
  f.backend.pollMcpOAuth = async () => {
    if (lastPoll) assert.ok(performance.now() - lastPoll >= 18)
    lastPoll = performance.now()
    return ++count > 2 ? { status: 'complete' } : { status: 'authorizing', device: { verification_uri: 'https://login.example.test/device', user_code: '1234', interval: 0.02 } }
  }
  await oauthConnection({ ...f, interval: 1, context: () => 'instance' }).connect('device', { transport: 'http', grant_type: 'device_code' })
  assert.equal(cancels, 0)
})

test('the browser opener refuses unsafe provider URLs before opening anything', async () => {
  const original = globalThis.open, opened = []
  globalThis.open = (...args) => opened.push(args)
  try {
    for (const url of ['javascript:alert(1)', 'http://login.example.test', 'https://secret@login.example.test']) {
      const f = hostedFixture({ status: 'authorizing', authorization: { id: 'web', callback_mode: 'web', authorization_url: url } })
      await assert.rejects(oauthConnection({ ...f, openBrowser: undefined, bridge: null, interval: 1, context: () => 'instance' }).connect('web', { transport: 'http', callback_mode: 'web' }), /HTTPS without credentials/)
      assert.ok(f.calls.some(c => c[0] === 'cancel'))
    }
    assert.deepEqual(opened, [])
    const f = hostedFixture({ status: 'authorizing', authorization: { id: 'web', callback_mode: 'web', authorization_url: 'https://login.example.test/authorize' } })
    await oauthConnection({ ...f, openBrowser: undefined, bridge: null, interval: 1, context: () => 'instance' }).connect('web', { transport: 'http', callback_mode: 'web' })
    assert.deepEqual(opened, [['https://login.example.test/authorize', '_blank', 'noopener,noreferrer']])
  } finally { globalThis.open = original }
})
