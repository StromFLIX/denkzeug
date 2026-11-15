// Write-only credentials are never populated from sanitized backend profiles.
export function hostedOAuthDraft (profile = {}) {
  const defaults = {
    client_id: '', redirect_uri: '', scope: '', response_mode: '',
    grant_type: 'authorization_code', token_endpoint_auth_method: 'none', callback_mode: 'desktop',
    client_secret: '', private_key: '', key_id: '', issuer: '', metadata_mode: 'discovery',
    authorization_endpoint: '', token_endpoint: '', device_authorization_endpoint: '',
    resource_parameter: '', resource_value: '', registration_access_token: '',
    registration_method: 'auto', token_endpoint_auth_signing_alg: '', token_expiry_seconds: '',
  }
  for (const key of Object.keys(defaults)) {
    if (!['client_secret', 'private_key', 'registration_access_token'].includes(key)) defaults[key] = profile[key] ?? defaults[key]
  }
  return defaults
}

const authorizationKeys = new Set(['prompt', 'login_hint', 'domain_hint', 'access_type', 'audience', 'organization'])
export function parseAuthorizationParams (text) {
  if (!text.trim()) return {}
  try {
    const value = JSON.parse(text)
    return value && typeof value === 'object' && !Array.isArray(value) &&
      Object.entries(value).every(([key, entry]) => authorizationKeys.has(key) && typeof entry === 'string' && !entry.includes('\u0000'))
      ? value : null
  } catch { return null }
}

export function hostedOAuthPayload (draft, authorizationParams) {
  if (authorizationParams === null) throw new Error('Enter a JSON object of supported authorization parameters with string values.')
  return {
    ...Object.fromEntries(Object.entries(draft).filter(([key, value]) =>
      !['client_secret', 'private_key', 'registration_access_token', 'resource_parameter', 'token_expiry_seconds'].includes(key) || (value !== '' && value !== null && value !== undefined))),
    authorization_params: authorizationParams,
  }
}
