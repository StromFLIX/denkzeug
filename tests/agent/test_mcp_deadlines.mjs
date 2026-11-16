import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { test } from 'node:test'
import { createMcpTransport } from '../../images/agent-sets/default/extensions/nautionette/mcp-transport.ts'

function fixture(tool) {
  const calls = []
  const transport = createMcpTransport('http://gateway/mcp', async (_url, options) => {
    if (options.method === 'DELETE') return new Response(null, { status: 204 })
    const request = JSON.parse(options.body)
    if (request.method === 'initialize') return Response.json({ result: {} }, { headers: { 'mcp-session-id': 'fixture' } })
    if (request.method === 'notifications/initialized') return new Response(null, { status: 202 })
    calls.push(request)
    return tool(request, options)
  })
  transport.setReadOnlyTools(['read'])
  return { transport, calls }
}

for (const fresh of [false, true]) test(`request deadline survives fresh=${fresh}, never retries even a read-only call`, async () => {
  let aborted = false
  const { transport, calls } = fixture((_request, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => { aborted = true; reject(options.signal.reason) }, { once: true })
  }))
  await assert.rejects(transport[fresh ? 'rpcFresh' : 'rpc']('tools/call', { name: 'read' }, undefined, 1), /read timed out after 1s/)
  assert.equal(aborted, true)
  assert.equal(calls.length, 1)
})

test('timeouts cover a stalled response body, not only waiting for HTTP headers', async () => {
  let calls = 0
  const server = createServer(async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk
    const body = JSON.parse(raw)
    if (body.method === 'notifications/initialized') { res.writeHead(202); return res.end() }
    res.writeHead(200, { 'content-type': 'application/json', 'mcp-session-id': 'body-session' })
    if (body.method === 'initialize') return res.end(JSON.stringify({ result: {} }))
    calls++
    res.flushHeaders()
    setTimeout(() => { if (!res.destroyed) res.end(JSON.stringify({ result: { ok: true } })) }, 1500).unref()
  })
  server.listen(0, '127.0.0.1'); await once(server, 'listening')
  try {
    const transport = createMcpTransport(`http://127.0.0.1:${server.address().port}/mcp`)
    await assert.rejects(transport.rpc('tools/call', { name: 'write_once' }, undefined, 1), /write_once timed out after 1s/)
    assert.equal(calls, 1)
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)) }
})

test('bounded diagnostics never echo private HTTP or JSON-RPC error messages', async () => {
  const cases = [
    [500, { error: { code: -32603, message: 'failed to send message', data: 'SECRET' } }, /gateway_send_failed/],
    [500, { error: { code: -32603, message: 'SECRET' } }, /HTTP 500/],
    [500, { error: { code: -32603, message: 'SECRET'.repeat(10000) } }, /HTTP 500/],
    [504, { error: { message: 'SECRET' } }, /gateway\/upstream timeout reported/],
    [200, { error: { code: -32603, message: 'SECRET' } }, /JSON-RPC -32603/]
  ]
  for (const [status, payload, expected] of cases) {
    const { transport, calls } = fixture(() => Response.json(payload, { status }))
    await assert.rejects(transport.rpc('tools/call', { name: 'read' }), error => {
      assert.match(error.message, expected)
      assert.doesNotMatch(error.message, /SECRET/)
      if (status === 500) assert.doesNotMatch(error.message, /timed out|timeout/)
      return true
    })
    assert.equal(calls.length, 1)
  }
})

test('safe stale-session recovery does not reset the original request deadline', async () => {
  const { transport, calls } = fixture(async () => {
    await new Promise(resolve => setTimeout(resolve, 600))
    return calls.length === 1 ? new Response('', { status: 404 }) : Response.json({ result: { ok: true } })
  })
  await assert.rejects(transport.rpc('tools/call', { name: 'read' }, undefined, 1), /timed out after 1s/)
  assert.equal(calls.length, 2) // Only the known stale read was replayed, never the deadline.
})

test('invalid override fails before any upstream call', async () => {
  const { transport, calls } = fixture(() => assert.fail('must not dispatch'))
  for (const seconds of [0, -1, 601, '120', true, 1.1])
    await assert.rejects(transport.rpc('tools/call', { name: 'read' }, undefined, seconds), /integer from 1 to 600/)
  assert.equal(calls.length, 0)
})
