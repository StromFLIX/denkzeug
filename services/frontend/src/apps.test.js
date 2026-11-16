import assert from 'node:assert/strict'
import { test } from 'node:test'
import { appDraft, appPayload, exportAppPackage, importAppPackage, parseAppObject, MAX_APP_PACKAGE_BYTES } from './apps.js'
import { api } from './api.js'
import { SETTINGS_SECTIONS, searchSettings } from './settings-registry.js'
import { memberSettingKeys } from './account.js'

globalThis.localStorage = { getItem: () => null }
const source = { id: 'report-1', title: 'Report', description: 'A report', html: '<!doctype html><h1>Report</h1>', input_schema: { type: 'object' }, sample_data: { count: 4 } }

test('app drafts round-trip source without mutating it and publish with exact optimistic revision', () => {
  const draft = appDraft(source)
  draft.title = ' Updated report '; draft.change_note = ' New chart '
  assert.deepEqual(appPayload(draft, 7), { ...source, title: 'Updated report', change_note: 'New chart', expected_revision: 7 })
  assert.equal(source.title, 'Report')
  assert.equal(appPayload(appDraft(source)).expected_revision, 0)
})

test('packages contain portable source, not revision IDs, account state or mutation preconditions', () => {
  const json = exportAppPackage({ ...source, owner_id: 'secret', active_revision: 7, archived: true, expected_revision: 7 })
  assert.deepEqual(JSON.parse(json), { version: 1, ...source })
  assert.deepEqual(importAppPackage(json), appDraft(source))
  assert.deepEqual(importAppPackage(JSON.stringify(source)), appDraft(source))
})

test('invalid packages and source JSON fail before preview or publish', () => {
  for (const [value, pattern] of [['{', /valid JSON/], ['null', /JSON object/], ['[]', /JSON object/]]) assert.throws(() => parseAppObject(value, 'Sample data'), pattern)
  for (const id of ['', '../report', 'Uppercase', 'double--dash', 'white space', 'a/b']) assert.throws(() => appPayload({ ...appDraft(source), id }), /slug/)
  assert.throws(() => appPayload({ ...appDraft(source), title: ' ' }), /title/)
  assert.throws(() => appPayload({ ...appDraft(source), html: ' ' }), /HTML/)
  assert.throws(() => importAppPackage('{'), /valid JSON/)
  assert.throws(() => importAppPackage(JSON.stringify({ ...source, html: 5 })), /HTML/)
  assert.throws(() => importAppPackage(JSON.stringify({ ...source, sample_data: [] })), /Sample data/)
  assert.throws(() => importAppPackage(JSON.stringify({ ...source, input_schema: null })), /Input schema|JSON object/)
  assert.throws(() => importAppPackage(JSON.stringify({ ...source, version: 99 })), /Unsupported/)
  assert.throws(() => importAppPackage('é'.repeat(MAX_APP_PACKAGE_BYTES)), /2 MB/)
})

test('Apps is an owner-scoped Settings contribution discoverable before mounting, including for members', () => {
  const section = SETTINGS_SECTIONS.find(section => section.key === 'apps')
  assert.equal(section.scope, 'Your account')
  assert.ok(memberSettingKeys.has('apps'))
  assert.ok(searchSettings('immutable').some(item => item.section.key === 'apps'))
  assert.ok(searchSettings('sandboxed').some(item => item.id === 'apps-permissions'))
})

test('app APIs encode identifiers, use same-origin auth and retain exact immutable-revision bodies', async t => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (path, options) => { calls.push({ path, ...options }); return Response.json({ ok: true }) })
  const payload = appPayload(appDraft(source), 2)
  await api.apps(); await api.app('a/b'); await api.appRevision('a/b', 2)
  await api.publishApp(payload); await api.editApp('a/b', { archived: true, expected_revision: 2 })
  assert.deepEqual(calls.map(call => call.path), ['/api/apps', '/api/apps/a%2Fb', '/api/apps/a%2Fb/revisions/2', '/api/apps', '/api/apps/a%2Fb'])
  assert.equal(calls[3].method, 'POST'); assert.equal(calls[4].method, 'PATCH')
  assert.deepEqual(JSON.parse(calls[3].body), payload)
  assert.deepEqual(JSON.parse(calls[4].body), { archived: true, expected_revision: 2 })
  assert.ok(calls.every(call => call.credentials === 'same-origin'))
})
