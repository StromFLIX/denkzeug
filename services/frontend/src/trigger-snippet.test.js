import assert from 'node:assert/strict'
import { test } from 'node:test'
import { spawnSync } from 'node:child_process'
import { shellQuote, triggerLanguages, triggerPreviewUrl, triggerSnippet } from './trigger-snippet.js'

const url = 'https://example.test/api/triggers/a%2Fb?token=dedicated%2Bworkflow%26secret'
const inputs = JSON.parse('{"message":"O\'Reilly \\"quoted\\"; $(touch /tmp/unsafe)\\nnext","enabled":true,"missing":null,"list":[false,3,{"city":"東京 🦊"}],"__proto__":{"safe":true},"escape":"\\\\n"}')

test('all four formats use placeholder-only previews and encode workflow names', () => {
  assert.deepEqual(triggerLanguages, ['curl', 'python', 'node', 'GET'])
  const preview = triggerPreviewUrl('https://example.test/', 'workflow/name? &')
  assert.equal(preview, 'https://example.test/api/triggers/workflow%2Fname%3F%20%26?token=YOUR_WORKFLOW_TOKEN')
  for (const format of triggerLanguages) {
    const text = triggerSnippet(format, preview, inputs)
    assert.match(text, /YOUR_WORKFLOW_TOKEN/)
    assert.doesNotMatch(text, /Authorization|Bearer|session/)
  }
  assert.throws(() => triggerSnippet('unsupported', url, {}), /supported/)
})

test('curl explicitly POSTs shell-quoted JSON and URL without executing input', () => {
  assert.equal(shellQuote("it's"), "'it'\\''s'")
  const text = triggerSnippet('curl', url, inputs)
  assert.match(text, /^curl -X POST /)
  // Capture shell arguments with a curl stand-in. No network request is sent.
  const result = spawnSync('sh', ['-c', `curl () { printf '%s\\0' "$@"; }; ${text}`], { encoding: 'utf8' })
  assert.equal(result.status, 0, result.stderr)
  const args = result.stdout.split('\0').slice(0, -1)
  assert.deepEqual(args, ['-X', 'POST', url, '-H', 'Content-Type: application/json', '--data-raw', JSON.stringify(inputs)])
})

test('Python POST uses json.loads for JSON booleans, null, nested input and quoting', () => {
  const text = triggerSnippet('python', url, inputs)
  assert.match(text, /httpx.post\(/)
  assert.match(text, /json=json.loads\(/)
  const harness = `import sys, types, json\nhttpx = types.ModuleType('httpx')\ndef post(url, json):\n    print(__import__('json').dumps({'url':url, 'input':json}))\n    return types.SimpleNamespace(raise_for_status=lambda: None)\nhttpx.post = post\nsys.modules['httpx'] = httpx\n`
  const result = spawnSync('python3', ['-c', harness + text], { encoding: 'utf8' })
  assert.equal(result.status, 0, result.stderr)
  assert.deepEqual(JSON.parse(result.stdout), { url, input: inputs })
})

test('Node POST serializes data without interpreting __proto__ or strings as code', async () => {
  const text = triggerSnippet('node', url, inputs)
  let call
  const run = new Function('fetch', `return (async () => { ${text} })()`)
  await run(async (requestUrl, options) => { call = { url: requestUrl, ...options }; return { ok: true } })
  assert.equal(call.url, url)
  assert.equal(call.method, 'POST')
  assert.deepEqual(call.headers, { 'Content-Type': 'application/json' })
  assert.deepEqual(JSON.parse(call.body), inputs)
  await assert.rejects(run(async () => ({ ok: false, status: 403 })), /HTTP 403/)
})

test('GET preserves the workflow token and round-trips complete URL-encoded JSON input', () => {
  const link = new URL(triggerSnippet('GET', url, inputs))
  assert.equal(link.searchParams.get('token'), 'dedicated+workflow&secret')
  assert.deepEqual(JSON.parse(link.searchParams.get('input')), inputs)
  assert.equal([...link.searchParams.keys()].length, 2)
  assert.deepEqual(JSON.parse(new URL(triggerSnippet('GET', url)).searchParams.get('input')), {})
})
