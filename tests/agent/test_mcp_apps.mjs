import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { appAuthoringSchema, appAuthoringInput, appAuthoringResult } from '../../images/agent-sets/default/extensions/nautionette/app-authoring.ts'
import { APP_MIME, APP_CAPABILITIES, modelResult, modelVisible, snapshotApp } from '../../images/agent-sets/default/extensions/nautionette/mcp-apps.ts'

test('file-backed publication preserves backend validation and avoids bundle echo', async () => {
  const schema = { type: 'object', properties: { body: { $ref: '#/$defs/AppPublish' } }, $defs: { AppPublish: { type: 'object', properties: { html: { type: 'string' } }, required: ['html', 'id'] } } }
  const tool = { name: 'backend_publish_app', inputSchema: schema }
  const augmented = appAuthoringSchema(tool)
  assert.ok(augmented.$defs.AppPublish.properties.html_path)
  assert.deepEqual(schema.$defs.AppPublish.required, ['html', 'id'])
  assert.equal(appAuthoringSchema(tool, true), schema)
  const directory = await mkdtemp(join(tmpdir(), 'denkzeug-app-'))
  try {
    const path = join(directory, 'view.html')
    await writeFile(path, '<html>bundled SDK</html>')
    const input = { body: { id: 'report', html_path: path } }
    assert.deepEqual(await appAuthoringInput(tool.name, input), { body: { id: 'report', html: '<html>bundled SDK</html>' } })
    await assert.rejects(appAuthoringInput(tool.name, input, true))
    await assert.rejects(appAuthoringInput(tool.name, { body: { ...input.body, html: 'ambiguous' } }))
    const receipt = appAuthoringResult(tool.name, { content: [{ type: 'text', text: JSON.stringify({ revision: { revision: 1, html: 'compiled bundle' } }) }] })
    assert.doesNotMatch(receipt.content[0].text, /compiled bundle/)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

const uri = 'ui://weather+forecast/view'
const tool = { name: 'weather_forecast', _meta: { ui: { resourceUri: uri } } }

test('UI metadata advertises supported MIME and enforces model visibility', () => {
  assert.deepEqual(APP_CAPABILITIES.extensions['io.modelcontextprotocol/ui'].mimeTypes, [APP_MIME])
  assert.equal(modelVisible(tool), true)
  assert.equal(modelVisible({ _meta: { ui: { visibility: ['app'] } } }), false)
  assert.equal(modelVisible({ _meta: { ui: { visibility: ['model'] } } }), true)
})

test('private metadata and HTML never enter model-visible fallback', () => {
  assert.equal(modelResult({ content: [], structuredContent: { count: 2 }, _meta: { secret: 'private' } }), '{\n  "count": 2\n}')
  assert.doesNotMatch(modelResult({ content: [], _meta: { secret: 'private' } }), /secret|private/)
  assert.equal(modelResult({ content: [{ type: 'text', text: 'Original' }], _meta: { secret: 'private' } }), 'Original')
})

test('snapshot reads the exact server-scoped resource and preserves private UI result', async () => {
  const result = { content: [], structuredContent: { temp: 12 }, _meta: { map: [1, 2] } }
  const args = { city: 'Zürich' }
  const html = '<html><body>Zürich</body></html>'
  let calls = 0
  const rpc = async (method, params) => {
    calls++
    assert.equal(method, 'resources/read')
    assert.deepEqual(params, { uri })
    return { contents: [{ uri, mimeType: APP_MIME, blob: Buffer.from(html).toString('base64') }] }
  }
  const snapshot = await snapshotApp(rpc, tool, args, result)
  assert.equal(snapshot.html, html)
  assert.deepEqual(snapshot.result, result)
  assert.deepEqual(snapshot.input, args)
  assert.equal(calls, 1)
  assert.equal(await snapshotApp(rpc, {}, args, result), undefined)
  assert.equal(await snapshotApp(rpc, tool, args, { isError: true }), undefined)
  assert.equal(calls, 1)
})

test('wrong MIME, resource origin and oversized HTML are rejected without tool replay', async () => {
  for (const item of [
    { uri, mimeType: 'text/html', text: '<html></html>' },
    { uri: 'ui://other+forecast/view', mimeType: APP_MIME, text: '<html></html>' },
    { uri, mimeType: APP_MIME, text: 'x'.repeat(1048577) }
  ]) {
    await assert.rejects(snapshotApp(async method => {
      assert.equal(method, 'resources/read')
      return { contents: [item] }
    }, tool, {}, { content: [] }))
  }
})
