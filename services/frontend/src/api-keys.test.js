import assert from 'node:assert/strict'
import { test } from 'node:test'
import { dailyTokenUsage, tokenPayload, tokenState } from './api-keys.js'
import { memberSettingKeys } from './account.js'
import { searchSettings } from './settings-registry.js'

const catalog = {
  policy: { default_days: 90, max_days: 365, webhook_days: 7 },
  scopes: [{ scope: 'workflows:run', resource_kind: 'workflow' }, { scope: 'profile:read', resource_kind: null }],
  resources: [{ kind: 'workflow', id: 'immutable-uuid', name: 'digest' }, { kind: 'tool', id: 'other', name: 'Tool' }]
}
test('builds least-privilege grants using immutable IDs and Unix expiry', () => {
  assert.deepEqual(tokenPayload(' Report ', 7, { 'workflows:run': { resources: ['immutable-uuid', 'immutable-uuid'] }, 'profile:read': {} }, catalog, 100.9), {
    name: 'Report', expires_at: 100 + 7 * 86400, grants: [{ scope: 'workflows:run', resources: ['immutable-uuid'] }, { scope: 'profile:read', resources: null }]
  })
  assert.deepEqual(tokenPayload('All', 90, { 'workflows:run': { all: true } }, catalog, 100).grants, [{ scope: 'workflows:run', resources: null }])
})
test('requires explicit permissions, resources and a bounded expiry', () => {
  for (const days of [0, 366, 1.5, NaN]) assert.throws(() => tokenPayload('Key', days, {}, catalog), /expiry/)
  assert.throws(() => tokenPayload(' ', 7, {}, catalog), /name/)
  assert.throws(() => tokenPayload('Key', 7, {}, catalog), /permission/)
  assert.throws(() => tokenPayload('Key', 7, { 'missing:scope': {} }, catalog), /Permissions changed/)
  for (const resources of [[], ['digest'], ['other']]) assert.throws(() => tokenPayload('Key', 7, { 'workflows:run': { resources } }, catalog), /resource/)
})
test('revocation takes precedence over expiry, including a zero timestamp', () => {
  assert.equal(tokenState({ expires_at: 200, revoked_at: null }, 100), 'Active')
  assert.equal(tokenState({ expires_at: 100, revoked_at: null }, 100), 'Expired')
  assert.equal(tokenState({ expires_at: 200, revoked_at: 0 }, 100), 'Revoked')
})
test('daily usage fills quiet UTC days and excludes old or future buckets', () => {
  const days = dailyTokenUsage([{ day: 86400, requests: 2, failures: 1 }, { day: 5 * 86400, requests: 9, failures: 0 }], 3 * 86400 + 100, 3)
  assert.deepEqual(days, [{ day: 86400, requests: 2, failures: 1 }, { day: 2 * 86400, requests: 0, failures: 0 }, { day: 3 * 86400, requests: 0, failures: 0 }])
})
test('members can discover API keys and webhook revocation in settings search', () => {
  assert.equal(memberSettingKeys.has('api-keys'), true)
  for (const query of ['API keys', 'webhook revoke', 'key activity', 'access token']) assert.ok(searchSettings(query).some(item => item.section.key === 'api-keys'))
})
