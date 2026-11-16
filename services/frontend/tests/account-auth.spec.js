import { test, expect } from '@playwright/test'

const user = (id = 'alice', role = 'admin') => ({ id, username: id, email: `${id}@example.test`, display_name: id === 'alice' ? 'Alice' : 'Bob', role, status: 'active', must_change_password: false, email_verified: true })
const me = value => ({ user: value, csrf_token: `csrf-${value.id}`, session_id: `session-${value.id}` })
async function fixture (page, state = {}) {
  state.calls = []
  state.policy ||= { registration_mode: 'invite_only', mail_available: true }
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method()
    const body = request.postData() ? request.postDataJSON() : null
    state.calls.push({ path, method, body, headers: request.headers() })
    const json = (value, status = 200) => route.fulfill({ json: value, status })
    if (path === '/api/auth/meta') return json({ setup_required: !!state.setup, ...state.policy, providers: [], native_min_version: 1 })
    if (path === '/api/auth/me') return state.user ? json(me(state.user)) : json({ detail: 'Sign in required' }, 401)
    if (path === '/api/auth/login') {
      if (body.password === 'wrong-password') return json({ detail: 'Username or password is incorrect.' }, 401)
      state.user = user(body.username, state.loginRole || 'admin'); state.user.must_change_password = !!state.mustChange
      return json({ ...me(state.user), ...(body.native ? { access_token: 'native-access', refresh_token: 'native-refresh' } : {}) })
    }
    if (path === '/api/auth/logout') { state.user = null; return json({ logged_out: true }) }
    if (path === '/api/auth/bootstrap') { state.user = user(body.username); return json(me(state.user)) }
    if (path === '/api/auth/invitation') return json({ email: 'invited@example.test', role: 'member', expires_at: '2030-01-01' })
    if (path === '/api/auth/register') { state.user = { ...user(body.username, 'member'), email: body.email || null, email_verified: false }; return json({ ...me(state.user), verification_required: false, ...(body.native ? { access_token: 'native-access', refresh_token: 'native-refresh' } : {}) }) }
    if (path === '/api/auth/verify-email') return json({ verified: true })
    if (path === '/api/auth/forgot-password' || path === '/api/auth/reset-password') return json({ message: 'Done' })
    if (path === '/api/auth/change-password') { state.mustChange = false; state.user = null; return json({ changed: true, signed_out: true }) }
    if (path === '/api/auth/profile') { Object.assign(state.user, body); return json(me(state.user)) }
    if (path === '/api/auth/sessions') return json({ sessions: [{ id: 'other', device: 'Another browser', last_seen_at: '2026-01-01', expires_at: '2030-01-01', current: false }] })
    if (path.startsWith('/api/auth/sessions/')) return json({ revoked: true })
    if (path === '/api/auth/users') return json({ users: [state.user, user('bob', 'member')] })
    if (path.endsWith('/recovery')) return json({ code: 'one-time-reset-code', link: 'https://workspace.test/#reset=one-time-reset-code', expires_at: 1893456000 })
    if (path.startsWith('/api/auth/users/')) return json({ updated: true })
    if (path === '/api/auth/invitations' && method === 'POST') return json({ invitation: { id: 'invite', ...body, status: 'pending', expires_at: '2030-01-01' }, code: 'new-invite-secret', link: 'https://workspace.test/#invite=new-invite-secret' })
    if (path === '/api/auth/invitations') return json({ invitations: [] })
    if (path === '/api/auth/reauth') { state.reauthed = true; return json({ ok: true }) }
    if (path === '/api/auth/policy') {
      if (method === 'PATCH') {
        if (state.challenge && !state.reauthed) return json({ detail: { code: 'reauthentication_required', message: 'Confirm your password' } }, 403)
        state.policy = { ...state.policy, ...body }
      }
      return json(state.policy)
    }
    if (path === '/api/system') return json({ components: [], agent_sets: [] })
    if (path === '/api/catalog') return json({ models: [], tools: [], agents: [], agent_sets: [], default_model: '' })
    if (path === '/api/chats') return json({ chats: [] })
    if (path === '/api/agents') return json({ agents: [] })
    if (path === '/api/projects') return json({ projects: [] })
    if (path === '/api/workflows') return json({ workflows: [] })
    if (path === '/api/drafts') return json({ drafts: [] })
    if (path === '/api/runs') return json({ runs: [] })
    if (path === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"kind":"connected"}\n\n' })
    if (path === '/api/mcp-oauth/status') return json({ connections: [] })
    return json({})
  })
  return state
}
async function signIn (page, username = 'alice') {
  await page.getByLabel('Username', { exact: true }).fill(username)
  await page.getByLabel('Password', { exact: true }).fill('long-password-123')
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
}

test('unauthenticated startup probes only auth, signs in with cookies, then logs out', async ({ page }) => {
  const state = await fixture(page)
  await page.goto('/settings/account')
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
  expect(state.calls.every(call => call.path.startsWith('/api/auth/'))).toBeTruthy()
  await signIn(page)
  await expect(page.getByRole('heading', { name: 'Profile & security' })).toBeVisible()
  await page.getByLabel('Display name', { exact: true }).fill('Alice edited')
  await page.getByRole('button', { name: 'Save profile', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Profile saved' })).toBeVisible()
  expect(state.calls.find(call => call.path === '/api/auth/profile').headers['x-csrf-token']).toBe('csrf-alice')
  expect(state.calls.some(call => call.headers.authorization)).toBeFalsy()
  await page.getByRole('button', { name: 'Sign out', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('nautionette.token'))).toBeNull()
})

test('login errors are visible and do not open workspace', async ({ page }) => {
  await fixture(page); await page.goto('/')
  await page.getByLabel('Username', { exact: true }).fill('alice')
  await page.getByLabel('Password', { exact: true }).fill('wrong-password')
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText('Username or password is incorrect.')
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeHidden()
})

test('bootstrap fragment is stripped, first administrator can join', async ({ page }) => {
  const state = await fixture(page, { setup: true })
  await page.goto('/#bootstrap=setup-secret')
  await expect(page.getByRole('heading', { name: 'Set up your workspace' })).toBeVisible()
  expect(page.url()).not.toContain('setup-secret')
  await page.getByLabel('Username', { exact: true }).fill('alice')
  await page.getByLabel('New password', { exact: true }).fill('new-password-123')
  await page.getByLabel('Confirm password', { exact: true }).fill('new-password-123')
  await page.getByRole('button', { name: 'Create administrator' }).click()
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible()
  expect(state.calls.find(call => call.path === '/api/auth/bootstrap').body.token).toBe('setup-secret')
  expect(state.calls.find(call => call.path === '/api/auth/bootstrap').body.email).toBeUndefined()
})

test('invitation link and manual code use the same registration flow', async ({ page }) => {
  const state = await fixture(page)
  await page.goto('/#invite=invitation-secret')
  await expect(page.getByText('Join this workspace as member.', { exact: false })).toBeVisible()
  expect(page.url()).not.toContain('invitation-secret')
  await page.getByLabel('Username', { exact: true }).fill('new-member')
  await page.getByLabel('New password', { exact: true }).fill('new-password-123')
  await page.getByLabel('Confirm password', { exact: true }).fill('new-password-123')
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible()
  const registration = state.calls.find(call => call.path === '/api/auth/register')
  expect(registration.body.code).toBe('invitation-secret')
  expect(registration.body.email).toBeUndefined()
  await page.goto('/settings/account')
  await page.getByRole('button', { name: 'Sign out', exact: true }).click()
  await page.getByRole('button', { name: 'Use an invitation' }).click()
  await expect(page.getByLabel('Invitation code or link', { exact: true })).toBeVisible()
})

test('email verification, forgot and reset flows never keep URL secrets', async ({ page }) => {
  const state = await fixture(page)
  await page.goto('/#verify=verify-secret')
  await expect(page.getByRole('status')).toContainText('recovery email verified')
  expect(page.url()).not.toContain('verify-secret')
  await page.getByRole('button', { name: 'Forgot password?' }).click()
  await page.getByLabel('Recovery email', { exact: true }).fill('unknown@example.test')
  await page.getByRole('button', { name: 'Send reset link' }).click()
  await expect(page.getByRole('status')).toContainText('If an account matches')
  await page.goto('/#reset=reset-secret')
  await page.getByLabel('New password', { exact: true }).fill('replacement-password-123')
  await page.getByLabel('Confirm password', { exact: true }).fill('replacement-password-123')
  await page.getByRole('button', { name: 'Reset password', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('Password reset')
  expect(state.calls.find(call => call.path === '/api/auth/reset-password').body.token).toBe('reset-secret')
})

test('required password change blocks workspace until completed', async ({ page }) => {
  const state = await fixture(page, { mustChange: true })
  await page.goto('/settings/account'); await signIn(page)
  await expect(page.getByRole('heading', { name: 'Change your password' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeHidden()
  await page.getByLabel('Current password', { exact: true }).fill('long-password-123')
  await page.getByLabel('New password', { exact: true }).fill('new-password-123')
  await page.getByLabel('Confirm password', { exact: true }).fill('new-password-123')
  await page.getByRole('button', { name: 'Change password', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
  await signIn(page)
  await expect(page.getByRole('heading', { name: 'Profile & security' })).toBeVisible()
  expect(state.calls.find(call => call.path === '/api/auth/change-password')).toBeTruthy()
})

test('member deep links, navigation, settings and startup are fail-closed', async ({ page }) => {
  const state = await fixture(page, { user: user('bob', 'member') })
  await page.goto('/settings/users')
  await expect(page).toHaveURL(/\/chats/)
  await page.goto('/settings/account')
  await expect(page.getByRole('heading', { name: 'Profile & security' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Main navigation' }).getByText('Workflows', { exact: true })).toBeHidden()
  await expect(page.getByRole('navigation', { name: 'Settings categories' }).getByText('Users', { exact: true })).toBeHidden()
  expect(state.calls.some(call => ['/api/system', '/api/settings', '/api/auth/users', '/api/mcp-oauth/status'].includes(call.path))).toBeFalsy()
})

test('recent-auth challenge confirms password before retrying admin policy change', async ({ page }) => {
  const state = await fixture(page, { user: user(), challenge: true })
  await page.goto('/settings/access')
  await page.getByRole('combobox', { name: 'Registration', exact: true }).selectOption('paused')
  await page.getByRole('button', { name: 'Save access policy' }).click()
  await expect(page.getByRole('heading', { name: 'Confirm it’s you' })).toBeVisible()
  await page.getByLabel('Password', { exact: true }).fill('long-password-123')
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Access policy saved' })).toBeVisible()
  expect(state.calls.filter(call => call.path === '/api/auth/policy' && call.method === 'PATCH')).toHaveLength(2)
})

test('admin invitation secret is transient and uses backend generated fragment link', async ({ page }) => {
  await fixture(page, { user: user() }); await page.goto('/settings/invitations')
  await page.getByLabel('Send to email (optional)', { exact: true }).fill('new@example.test')
  await page.getByRole('button', { name: 'Create invitation' }).click()
  await expect(page.getByLabel('Invitation link', { exact: true })).toHaveValue('https://workspace.test/#invite=new-invite-secret')
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('new-invite-secret')
  await page.getByRole('button', { name: 'Hide secret' }).click()
  await expect(page.getByLabel('Invitation code', { exact: true })).toBeHidden()
})

test('logging into another account cannot read prior cached chats or queued messages', async ({ page }) => {
  await fixture(page, { user: user() }); await page.goto('/settings/account')
  await expect(page.getByRole('heading', { name: 'Profile & security' })).toBeVisible()
  const oldScope = await page.evaluate(async () => {
    const { chatCache, chatCacheScope } = await import('/src/chat-cache.js')
    const scope = chatCacheScope()
    await chatCache.put({ chat: { id: 'alice-private', title: 'Private Alice conversation' }, messages: [] }, scope)
    localStorage.setItem(`nautionette.outbox.${encodeURIComponent(scope)}.alice-message`, JSON.stringify({ id: 'alice-message', chatId: 'alice-private', text: 'Alice queued secret', createdAt: 1, nextAttempt: Date.now() + 600000 }))
    return scope
  })
  await page.getByRole('button', { name: 'Sign out', exact: true }).click()
  await signIn(page, 'bob')
  await expect(page.getByRole('heading', { name: 'Profile & security' })).toBeVisible()
  const state = await page.evaluate(async () => {
    const { chatCache, chatCacheScope } = await import('/src/chat-cache.js')
    const { pendingMessages } = await import('/src/delivery.js')
    return { scope: chatCacheScope(), cached: await chatCache.get('alice-private'), pending: pendingMessages.value }
  })
  expect(state.scope).not.toBe(oldScope)
  expect(state.cached).toBeNull()
  expect(state.pending).toEqual([])
  await expect(page.getByText('Private Alice conversation', { exact: true })).toBeHidden()
})

test('auth screens fit a narrow viewport and remain keyboard reachable', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 700 }); await fixture(page); await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  await page.getByLabel('Username', { exact: true }).focus()
  await page.keyboard.press('Tab'); await expect(page.getByLabel('Password', { exact: true })).toBeFocused()
  await page.screenshot({ path: testInfo.outputPath('account-mobile.png') })
})

test('email-free invitations provide an explicit copy-code action', async ({ page }) => {
  const state = await fixture(page, { user: user(), policy: { registration_mode: 'invite_only', mail_available: false } })
  await page.goto('/settings/invitations')
  await page.getByLabel('Note (optional)').fill('For Sam')
  await page.getByRole('button', { name: 'Create invitation', exact: true }).click()
  await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue('new-invite-secret')
  const body = state.calls.find(call => call.path === '/api/auth/invitations' && call.method === 'POST').body
  expect(body.email).toBeUndefined(); expect(body.send_email).toBeFalsy(); expect(body.label).toBe('For Sam')
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { globalThis.copiedCode = value } } }))
  await page.getByRole('button', { name: 'Copy code', exact: true }).click()
  expect(await page.evaluate(() => globalThis.copiedCode)).toBe('new-invite-secret')
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('new-invite-secret')
})

test('manual password reset needs only a pasted administrator code, not email', async ({ page }) => {
  const state = await fixture(page, { policy: { registration_mode: 'invite_only', mail_available: false } })
  await page.goto('/')
  await page.getByRole('button', { name: 'Forgot password?', exact: true }).click()
  await expect(page.getByText('Ask an administrator for a one-time reset code', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: 'Use a reset code', exact: true }).click()
  await page.getByLabel('Reset code or link', { exact: true }).fill('admin-reset-code')
  await page.getByLabel('New password', { exact: true }).fill('a new code-only passphrase 728!')
  await page.getByLabel('Confirm password', { exact: true }).fill('a new code-only passphrase 728!')
  await page.getByRole('button', { name: 'Reset password', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible()
  expect(state.calls.find(call => call.path === '/api/auth/reset-password').body.token).toBe('admin-reset-code')
  expect(state.calls.some(call => call.path === '/api/auth/forgot-password')).toBeFalsy()
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('admin-reset-code')
})

test('administrators can create a transient recovery code for a member', async ({ page }) => {
  const state = await fixture(page, { user: user() })
  await page.goto('/settings/users')
  await page.locator('.account-list li').filter({ hasText: '@bob' }).getByRole('button', { name: 'Create reset code' }).click()
  await expect(page.getByLabel('Password reset code', { exact: true })).toHaveValue('one-time-reset-code')
  expect(state.calls.some(call => call.path === '/api/auth/users/bob/recovery')).toBeTruthy()
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('one-time-reset-code')
  await page.getByRole('button', { name: 'Hide reset code' }).click()
  await expect(page.getByLabel('Password reset code', { exact: true })).toBeHidden()
})
