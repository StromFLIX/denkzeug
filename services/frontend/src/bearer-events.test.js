import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BearerEventSource } from './bearer-events.js'
import { auth } from './auth-session.js'

test('native SSE parses split frames, named heartbeat and multiline data without URL credentials', async t => {
  auth.token = 'native-access-secret'
  const encoder = new TextEncoder(), messages = [], heartbeats = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, 'https://workspace.test/api/events')
    assert.deepEqual(options.headers, { Authorization: 'Bearer native-access-secret', Accept: 'text/event-stream' })
    assert.equal(options.credentials, 'omit')
    return new Response(new ReadableStream({ start (controller) {
      for (const data of ['event: heartbeat\r\nda', 'ta: ping\r\n\r\ndata: {"kind":\n', 'data: "connected"}\n\n']) controller.enqueue(encoder.encode(data))
    } }), { headers: { 'Content-Type': 'text/event-stream' } })
  })
  const source = new BearerEventSource('https://workspace.test/api/events')
  source.addEventListener('heartbeat', event => heartbeats.push(event.data))
  await new Promise((resolve, reject) => {
    source.onerror = reject
    source.onmessage = event => { messages.push(event.data); source.close(); resolve() }
  })
  assert.deepEqual(heartbeats, ['ping'])
  assert.deepEqual(messages.map(JSON.parse), [{ kind: 'connected' }])
  assert.equal(source.readyState, 2)
  assert.equal(source.controller.signal.aborted, true)
  auth.token = ''
})
