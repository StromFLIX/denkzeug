import { test } from 'node:test'
import assert from 'node:assert/strict'
import { hostedOAuthDraft, hostedOAuthPayload, parseAuthorizationParams } from './mcp-oauth-config.js'

test('hosted drafts default to desktop authorization code with discovery and public client auth', () => {
  const draft = hostedOAuthDraft()
  assert.equal(draft.grant_type, 'authorization_code')
  assert.equal(draft.callback_mode, 'desktop')
  assert.equal(draft.token_endpoint_auth_method, 'none')
  assert.equal(draft.metadata_mode, 'discovery')
  assert.equal(draft.resource_parameter, '')
  assert.equal('resource_parameter' in hostedOAuthPayload(draft, {}), false)
  assert.equal('token_expiry_seconds' in hostedOAuthPayload(draft, {}), false)
  assert.equal(draft.registration_method, 'auto')
})

test('stored profile settings round-trip without importing credentials or sanitized flags', () => {
  const draft = hostedOAuthDraft({ grant_type: 'client_credentials', token_endpoint_auth_method: 'private_key_jwt',
    metadata_mode: 'manual', token_endpoint: 'https://login.example.test/token', resource_parameter: 'audience',
    resource_value: 'api', key_id: 'key-1', issuer: 'https://login.example.test', client_secret_configured: true,
    private_key_configured: true, client_secret: 'must-not-copy', private_key: 'must-not-copy', registration_access_token: 'must-not-copy' })
  assert.equal(draft.private_key, '')
  assert.equal(draft.client_secret, '')
  assert.equal(draft.registration_access_token, '')
  assert.equal('private_key_configured' in draft, false)
  const payload = hostedOAuthPayload(draft, {})
  assert.equal('client_secret' in payload, false)
  assert.equal('private_key' in payload, false)
  assert.equal('registration_access_token' in payload, false)
  assert.equal(payload.token_endpoint_auth_method, 'private_key_jwt')
  assert.equal(payload.resource_parameter, 'audience')
  assert.equal(payload.key_id, 'key-1')
})

test('new write-only credentials are submitted once without trimming PEM or secret bytes', () => {
  const draft = hostedOAuthDraft()
  draft.client_secret = ' secret '
  draft.private_key = '-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n'
  draft.registration_access_token = 'registration'
  const payload = hostedOAuthPayload(draft, { prompt: 'consent' })
  assert.equal(payload.client_secret, draft.client_secret)
  assert.equal(payload.private_key, draft.private_key)
  assert.equal(payload.registration_access_token, 'registration')
  assert.deepEqual(payload.authorization_params, { prompt: 'consent' })
})

test('authorization parameters accept only supported string-valued keys in an object', () => {
  assert.deepEqual(parseAuthorizationParams(''), {})
  assert.deepEqual(parseAuthorizationParams('{"prompt":"consent","organization":"org","audience":"api","login_hint":"person@example.test","domain_hint":"example.test","access_type":"offline"}'),
    { prompt: 'consent', organization: 'org', audience: 'api', login_hint: 'person@example.test', domain_hint: 'example.test', access_type: 'offline' })
  for (const value of ['null', '[]', '"text"', '{', '{"client_secret":"bad"}', '{"state":"bad"}', '{"prompt":true}', '{"prompt":{}}', '{"prompt":"\\u0000"}']) {
    assert.equal(parseAuthorizationParams(value), null, value)
  }
  assert.throws(() => hostedOAuthPayload(hostedOAuthDraft(), null), /supported authorization parameters/)
})
