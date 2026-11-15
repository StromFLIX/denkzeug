import assert from 'node:assert/strict'
import { test } from 'node:test'
import { dailyTokenUsage, tokenDate, tokenExpiry, tokenExpiryDate, tokenPayload, tokenState } from './api-keys.js'
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
test('specific expiry dates are converted to Unix seconds and bounded', () => {
  assert.equal(tokenExpiryDate('2026-06-02T12:30:00Z', 365, Date.parse('2026-06-01T12:30:00Z') / 1000), Date.parse('2026-06-02T12:30:00Z') / 1000)
  for (const value of ['', 'not a date', '2020-01-01T00:00:00Z', '2035-01-01T00:00:00Z']) assert.throws(() => tokenExpiryDate(value, 365, Date.parse('2026-06-01T00:00:00Z') / 1000), /future expiry/)
})
test('revocation takes precedence over expiry, including a zero timestamp', () => {
  assert.equal(tokenState({ expires_at: 200, revoked_at: null }, 100), 'Active')
  assert.equal(tokenState({ expires_at: 100, revoked_at: null }, 100), 'Expired')
  assert.equal(tokenState({ expires_at: 200, revoked_at: 0 }, 100), 'Revoked')
})
test('never-expiring keys retain permissions, activity and revocation semantics', () => {
  const policy = { ...catalog, policy: { ...catalog.policy, allow_never_expire: true } }
  const selected = { 'workflows:run': { resources: ['immutable-uuid'] } }
  assert.deepEqual(tokenPayload('Long-lived', null, selected, policy, 100), {
    name: 'Long-lived', expires_at: null, grants: [{ scope: 'workflows:run', resources: ['immutable-uuid'] }]
  })
  assert.throws(() => tokenPayload('Key', null, selected, catalog), /does not allow/)
  assert.throws(() => tokenPayload('Key', null, {}, policy), /permission/)
  assert.equal(tokenState({ expires_at: null, revoked_at: null }, 100), 'Active')
  assert.equal(tokenState({ expires_at: null, revoked_at: 0 }, 100), 'Revoked')
  assert.equal(tokenExpiry(null), 'Never expires')
  assert.equal(tokenDate(null), 'Never used')
  assert.equal(tokenExpiry(100), `Expires ${tokenDate(100)}`)
})
test('daily usage fills quiet UTC days and excludes old or future buckets', () => {
  const days = dailyTokenUsage([{ day: 86400, requests: 2, failures: 1 }, { day: 5 * 86400, requests: 9, failures: 0 }], 3 * 86400 + 100, 3)
  assert.deepEqual(days, [{ day: 86400, requests: 2, failures: 1 }, { day: 2 * 86400, requests: 0, failures: 0 }, { day: 3 * 86400, requests: 0, failures: 0 }])
})
test('members can discover API keys and webhook revocation in settings search', () => {
  assert.equal(memberSettingKeys.has('api-keys'), true)
  for (const query of ['API keys', 'webhook revoke', 'key activity', 'access token']) assert.ok(searchSettings(query).some(item => item.section.key === 'api-keys'))
})
