import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setTimeout as sleep } from 'node:timers/promises'
import { MCP_TIMEOUT_TOOL, MCP_TIMEOUT_PRESETS, McpTimeoutError, currentMcpTimeout, registerMcpTimeoutTool, validateMcpTimeout, withMcpDeadline } from '../../images/agent-sets/default/extensions/nautionette/mcp-timeout.ts'

function wrapper(names = ['remote_read', 'remote_write']) {
  const tools = []
  registerMcpTimeoutTool({ registerTool: t => tools.push(t) }, names)
  return tools[0]
}

test('timeout wrapper publishes sensible presets without modifying remote schemas', async () => {
  assert.deepEqual(MCP_TIMEOUT_PRESETS, { fast: 30, normal: 120, long: 600 })
  assert.equal(wrapper([]), undefined)
  const tool = wrapper()
  assert.equal(tool.name, MCP_TIMEOUT_TOOL)
  assert.equal(tool.exposure, 'model-only')
  assert.deepEqual(tool.reachesTools({ tool: 'remote_read' }), ['remote_read'])
  assert.deepEqual(tool.reachesTools({ tool: 'denied' }), [])
  assert.match(tool.description, /120s/)
  for (const [options, expected] of [[{}, 120], [{ preset: 'fast' }, 30], [{ preset: 'normal' }, 120], [{ preset: 'long' }, 600], [{ timeout_seconds: 45 }, 45]]) {
    const args = { timeout_seconds: 'a real upstream argument', nested: { x: 1 } }
    const signal = new AbortController().signal
    const onUpdate = () => {}
    const result = await tool.execute('call', { tool: 'remote_read', arguments: args, ...options }, signal, onUpdate, {
      async executeTool(name, input, forwarded) {
        assert.equal(name, 'remote_read')
        assert.equal(input, args)
        assert.equal(forwarded.signal, signal)
        assert.equal(forwarded.onUpdate, onUpdate)
        assert.equal(currentMcpTimeout(), expected)
        return { result: { content: [{ type: 'text', text: 'ok' }], details: { safe: true }, usage: { cost: 1 } }, isError: false }
      }
    })
    assert.equal(result.content[0].text, 'ok')
    assert.equal(result.usage, undefined) // Pi already accounts for nested usage.
    assert.equal(currentMcpTimeout(), undefined)
  }
})

for (const value of [0, -1, 601, 1.1, true, '60', null, NaN, Infinity]) test(`invalid timeout ${value} fails before tool execution`, async () => {
  assert.throws(() => validateMcpTimeout(value), /integer from 1 to 600/)
  await assert.rejects(wrapper().execute('id', { tool: 'remote_read', arguments: {}, timeout_seconds: value }, undefined, undefined, {
    executeTool() { assert.fail('must not dispatch') }
  }), /integer from 1 to 600/)
})

test('wrapper cannot expand selection, call itself, or choose ambiguous options', async () => {
  for (const input of [{ tool: 'denied' }, { tool: MCP_TIMEOUT_TOOL }, { tool: 'remote_read', preset: 'huge' }, { tool: 'remote_read', preset: 'fast', timeout_seconds: 50 }]) {
    await assert.rejects(wrapper().execute('id', { arguments: {}, ...input }, undefined, undefined, { executeTool() { assert.fail('must not dispatch') } }))
  }
})

test('concurrent overrides stay invocation-local and propagate inner failures', async () => {
  const tool = wrapper()
  const results = await Promise.all([30, 600].map(seconds => tool.execute('id', { tool: 'remote_read', arguments: {}, timeout_seconds: seconds }, undefined, undefined, {
    async executeTool() {
      assert.equal(currentMcpTimeout(), seconds)
      await sleep(10)
      assert.equal(currentMcpTimeout(), seconds)
      return { result: { content: [{ type: 'text', text: 'original validation/permission failure' }], details: {} }, isError: true }
    }
  })))
  assert.ok(results.every(result => result.isError))
  assert.equal(currentMcpTimeout(), undefined)
})

test('deadline aborts the request and bounds the wait even if a transport ignores cancellation', async () => {
  let signal
  await assert.rejects(withMcpDeadline(s => { signal = s; return new Promise(() => {}) }, 0.02, 'MCP tool fixture_write'), error => {
    assert.ok(error instanceof McpTimeoutError)
    assert.match(error.message, /fixture_write.*timed out/)
    assert.match(error.message, /No automatic retry/)
    assert.match(error.message, /may still be running/)
    return true
  })
  assert.equal(signal.aborted, true)
})

test('caller cancellation is not mislabeled as a deadline, including an already cancelled call', async () => {
  const controller = new AbortController()
  const reason = new Error('caller stopped')
  const promise = withMcpDeadline(async signal => {
    await sleep(5)
    controller.abort(reason)
    await sleep(1000, undefined, { signal })
  }, 5, 'MCP tool fixture', controller.signal)
  await assert.rejects(promise, error => error === reason && !(error instanceof McpTimeoutError))
  await assert.rejects(withMcpDeadline(() => assert.fail('must not dispatch'), 1, 'fixture', controller.signal), error => error === reason)
})
