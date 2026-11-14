import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

test('member CLI policy rejects a model-supplied bash call rather than executing it', { timeout: 25000 }, async () => {
  const root = mkdtempSync(join(tmpdir(), 'denkzeug-member-policy-'))
  const config = join(root, 'config'), operation = join(root, 'must-not-exist')
  mkdirSync(config)
  const requests = []
  let child
  const server = createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk
    const request = JSON.parse(body); requests.push(request)
    res.writeHead(200, { 'content-type': 'text/event-stream' })
    const chunk = (delta, finish = null) => res.write(`data: ${JSON.stringify({ id: 'offline', object: 'chat.completion.chunk', model: 'test-model', choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`)
    chunk({ role: 'assistant', content: '' })
    if (requests.length === 1) {
      chunk({ tool_calls: [{ index: 0, id: 'denied', type: 'function', function: { name: 'bash', arguments: JSON.stringify({ command: `printf forbidden > '${operation}'` }) } }] })
      chunk({}, 'tool_calls')
    } else { chunk({ content: 'Local execution is unavailable.' }); chunk({}, 'stop') }
    res.end('data: [DONE]\n\n')
  })
  try {
    server.listen(0, '127.0.0.1'); await once(server, 'listening')
    writeFileSync(join(config, 'settings.json'), JSON.stringify({ retry: { enabled: false }, compaction: { enabled: false } }))
    const provider = join(root, 'provider.mjs')
    writeFileSync(provider, `export default pi => pi.registerProvider('nautionette', { baseUrl: 'http://127.0.0.1:${server.address().port}/v1', apiKey: 'offline-test', api: 'openai-completions', models: [{ id: 'test-model', name: 'Test', reasoning: false, input: ['text'], contextWindow: 32000, maxTokens: 2000, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }] });`)
    child = spawn('pi', ['--mode', 'json', '--no-session', '--provider', 'nautionette', '--model', 'test-model', '--approve',
      '--no-builtin-tools', '--exclude-tools', 'read,write,edit,bash,grep,find,ls,codemode,tool_search',
      '--no-extensions', '-e', provider, '--no-context-files', '--no-skills', '--no-prompt-templates', '--', 'Respect the member tool policy.'], {
      cwd: root, env: { PATH: process.env.PATH, HOME: root, PI_CODING_AGENT_DIR: config, PI_OFFLINE: '1', PI_SKIP_VERSION_CHECK: '1', PI_TELEMETRY: '0' }, stdio: ['ignore', 'pipe', 'pipe']
    })
    let error = ''; child.stderr.on('data', data => { error += data })
    child.stdout.resume()
    const [code] = await once(child, 'close')
    assert.equal(code, 0, error)
    assert.ok(requests.length >= 1)
    assert.ok(requests.every(request => !(request.tools || []).some(tool => ['bash', 'read', 'tool_search', 'codemode'].includes(tool.function?.name))))
    assert.equal(existsSync(operation), false, 'a fabricated tool call must never execute a local command')
  } finally {
    if (child && child.exitCode === null) child.kill('SIGKILL')
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve))
    rmSync(root, { recursive: true, force: true })
  }
})
