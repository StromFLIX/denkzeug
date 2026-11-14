import { test } from 'node:test'
import assert from 'node:assert/strict'
import { account, auth, acceptAccount, clearAccount, consumeAuthFragment, secureSession } from './auth-session.js'
import { request, setReauthHandler } from './api.js'

const me = id => ({ user: { id, username: id, status: 'active', role: 'member' }, csrf_token: 'csrf-secret', session_id: 'session' })
test('credentials stay memory-only and account epochs fence identity changes', () => {
  clearAccount()
  const epoch = account.epoch
  acceptAccount({ ...me('alice'), access_token: 'memory-only' })
  assert.equal(auth.token, 'memory-only')
  assert.equal(auth.scope, 'alice')
  assert.equal(account.epoch, epoch + 1)
  clearAccount()
  assert.equal(auth.token, '')
  assert.equal(auth.scope, 'signed-out')
  assert.throws(() => acceptAccount({ user: { id: 'alice', status: 'suspended' } }), /not active/)
})
test('auth fragment is removed before exchange, leaving unrelated anchors and query intact', () => {
  const history = { state: { key: 1 }, replaceState (...args) { this.args = args } }
  const result = consumeAuthFragment({ hash: '#invite=sensitive%2Bcode&section=account', pathname: '/chats', search: '?safe-appearance=1' }, history)
  assert.deepEqual(result, { flow: 'invite', token: 'sensitive+code' })
  assert.deepEqual(history.args, [{ key: 1 }, '', '/chats?safe-appearance=1#section=account'])
})
test('web cookie writes use CSRF and same-origin; never forward bearer tokens', async t => {
  acceptAccount(me('alice')); auth.token = 'must-not-leak'
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/api/auth/profile')
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.headers['X-CSRF-Token'], 'csrf-secret')
    assert.equal(options.headers.Authorization, undefined)
    return Response.json({ ok: true })
  })
  await request('/api/auth/profile', { method: 'PATCH', body: '{}' })
  clearAccount()
})
test('an old response cannot update the next account', async t => {
  acceptAccount(me('alice'))
  let finish
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => { finish = resolve }))
  const pending = request('/api/chats')
  clearAccount(); acceptAccount(me('bob'))
  finish(Response.json({ chats: [{ title: 'Alice private content' }] }))
  await assert.rejects(pending, error => error.status === 409)
  clearAccount()
})
test('account fencing also covers response bodies that arrive after their headers', async t => {
  acceptAccount(me('alice'))
  let finishBody, bodyStarted
  const started = new Promise(resolve => { bodyStarted = resolve })
  t.mock.method(globalThis, 'fetch', async () => ({ ok: true, status: 200, json: () => { bodyStarted(); return new Promise(resolve => { finishBody = resolve }) } }))
  const pending = request('/api/chats')
  await started
  clearAccount(); acceptAccount(me('bob'))
  finishBody({ chats: [{ title: 'Alice private content' }] })
  await assert.rejects(pending, error => error.status === 409)
  clearAccount()
})

test('recent-auth challenge resumes once after password verification, not on cancel', async t => {
  acceptAccount(me('alice'))
  let calls = 0, challenges = 0
  t.mock.method(globalThis, 'fetch', async () => ++calls === 1 ? Response.json({ detail: { code: 'reauthentication_required', message: 'Confirm password.' } }, { status: 403 }) : Response.json({ saved: true }))
  setReauthHandler(async () => { challenges++ })
  assert.deepEqual(await request('/api/auth/policy', { method: 'PATCH', body: '{}' }), { saved: true })
  assert.equal(calls, 2); assert.equal(challenges, 1)
  calls = 0
  setReauthHandler(async () => { throw new Error('Cancelled') })
  await assert.rejects(request('/api/auth/policy', { method: 'PATCH', body: '{}' }), /Cancelled/)
  assert.equal(calls, 1)
  setReauthHandler(null); clearAccount()
})
test('secure persistence fails closed if native bridge is missing', async () => {
  await assert.rejects(secureSession('set', 'https://example.test', 'refresh-secret'), /Secure account storage is unavailable/)
})
