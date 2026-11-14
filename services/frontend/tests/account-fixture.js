import { test as base, expect } from '@playwright/test'

export const fixtureAccount = { user: { id: 'test-admin', username: 'admin', email: 'admin@example.test', display_name: 'Test administrator', role: 'admin', status: 'active', email_verified: true, must_change_password: false }, csrf_token: 'fixture-csrf', session_id: 'fixture-session' }
export function fulfillAccount (route) {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/auth/meta') return route.fulfill({ json: { setup_required: false, registration_mode: 'invite_only', mail_available: true, providers: [], native_min_version: 1 } })
  if (['/api/auth/me', '/api/auth/login', '/api/auth/refresh'].includes(path)) {
    const native = path.endsWith('/refresh') || route.request().postDataJSON()?.native
    return route.fulfill({ json: { ...fixtureAccount, ...(native ? { access_token: 'fixture-native-access', refresh_token: 'fixture-native-refresh' } : {}) } })
  }
  if (path === '/api/auth/sessions') return route.fulfill({ json: { sessions: [] } })
  if (path === '/api/auth/policy') return route.fulfill({ json: { registration_mode: 'invite_only', mail_available: true } })
  return route.fulfill({ json: {} })
}

// Existing feature tests exercise an authenticated workspace, never an auth bypass
// in application code. Page-level routes take precedence over their API fixtures.
export const test = base.extend({
  context: async ({ context }, use) => {
    await context.addInitScript(() => {
      let desktop
      const accountStore = { get: async () => ({ value: 'fixture-native-refresh' }), set: async () => ({}), remove: async () => ({}) }
      Object.defineProperty(globalThis, 'denkzeugDesktop', { configurable: true, get: () => desktop || (globalThis.nautionetteDesktop ? { ...globalThis.nautionetteDesktop, accountStore } : globalThis.Capacitor?.isNativePlatform?.() ? { accountStore } : undefined), set: value => { desktop = { ...value, accountStore: value?.accountStore || accountStore } } })
    })
    context.on('page', page => page.route('**/api/auth/**', fulfillAccount))
    await use(context)
  }
})
export { expect }
