import { expect, test, fixtureAccount } from './account-fixture.js'
import { mockDesign } from './design-fixture.js'

async function setup (page, context, { member = false, empty = false, clipboardFails = false } = {}) {
  await mockDesign(context)
  if (member) await page.route('**/api/auth/me', route => route.fulfill({ json: { ...fixtureAccount, user: { ...fixtureAccount.user, role: 'member' } } }))
  await context.addInitScript(fails => {
    window.copiedTokens = []
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { if (fails) throw new Error('Clipboard denied'); window.copiedTokens.push(text) } } })
    if (fails) document.execCommand = () => false
  }, clipboardFails)
  const now = Math.floor(Date.now() / 1000)
  const catalog = {
    scopes: [{ scope: 'workflows:run', description: 'Run a workflow', resource_kind: 'workflow' }, { scope: 'profile:read', description: 'Read your profile', resource_kind: null }],
    policy: { default_days: 90, max_days: 365, webhook_days: 7 },
    resources: [{ kind: 'workflow', id: 'immutable-digest-id', name: 'Morning digest' }]
  }
  const token = { id: 'existing', name: 'Daily report', prefix: 'dz_report', kind: 'api_key', grants: [{ scope: 'workflows:run', resources: ['immutable-digest-id'] }], created_at: now - 86400, expires_at: now + 86400 * 90, revoked_at: null, last_used_at: now, requests_7d: 123, failures_7d: 2 }
  const state = { tokens: empty ? [] : [token], creates: [], rotations: [], revokes: [], offsets: [], webhooks: [], requestFailure: false, createFailure: false, webhookFailure: false }
  await page.route('**/api/tokens**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method()
    if (path === '/api/tokens/catalog') return route.fulfill({ json: catalog })
    if (path === '/api/tokens' && method === 'GET') return route.fulfill({ json: { tokens: state.tokens } })
    if (path === '/api/tokens' && method === 'POST') {
      const body = request.postDataJSON(); state.creates.push(body)
      if (state.createFailure) return route.fulfill({ status: 403, json: { detail: 'Permission no longer available' } })
      const created = { ...token, ...body, id: 'created', prefix: 'dz_new', requests_7d: 0, failures_7d: 0, last_used_at: null }
      state.tokens.push(created)
      return route.fulfill({ json: { token: created, secret: 'one-time-api-secret' } })
    }
    if (path.endsWith('/rotate')) {
      state.rotations.push(path); state.tokens[0].revoked_at = now
      const rotated = { ...state.tokens[0], id: 'rotated', name: 'Daily report replacement', revoked_at: null }
      state.tokens.push(rotated)
      return route.fulfill({ json: { token: rotated, secret: 'rotated-once-secret' } })
    }
    if (method === 'DELETE') { state.revokes.push(path); state.tokens.find(item => path.endsWith(item.id)).revoked_at = now; return route.fulfill({ json: { ok: true } }) }
    if (path.endsWith('/usage')) return route.fulfill({ json: { days: [{ day: Math.floor(now / 86400) * 86400, requests: 12, failures: 2 }], retention: { detail_days: 90, aggregate_days: 365 } } })
    if (path.endsWith('/requests')) {
      if (state.requestFailure) return route.fulfill({ status: 503, json: { detail: 'Request history unavailable' } })
      const offset = Number(url.searchParams.get('offset')); state.offsets.push(offset)
      return route.fulfill({ json: { total: 51, requests: [{ id: `request-${offset}`, request_id: `trace-${offset}`, created_at: now, method: 'POST', route: '/api/workflows/digest/run', resource_id: 'immutable-digest-id', required_scope: 'workflows:run', status: 200, duration_ms: 25, outcome: 'allowed', tool: null, run_id: 'run-123' }] } })
    }
    return route.fulfill({ json: {} })
  })
  await page.route('**/api/workflows/digest/webhook-token', async route => {
    state.webhooks.push(route.request().postDataJSON())
    if (state.webhookFailure) return route.fulfill({ status: 403, json: { detail: 'Workflow access removed' } })
    const webhook = { ...token, id: 'webhook', name: 'Morning digest webhook', kind: 'webhook', expires_at: now + 7 * 86400 }
    state.tokens.push(webhook)
    return route.fulfill({ json: { token: webhook, secret: 'webhook-once', url: 'https://example.test/api/triggers/digest?token=webhook-once', expires_at: webhook.expires_at } })
  })
  return state
}

for (const width of [1440, 390]) {
  test(`member key inventory, resource grants and one-time reveal at ${width}px`, async ({ page, context }, testInfo) => {
    const state = await setup(page, context, { member: true, empty: true })
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/settings/api-keys')
    await expect(page.getByRole('heading', { name: 'API keys', exact: true })).toBeVisible()
    await expect(page.getByText('No keys yet', { exact: true })).toBeVisible()
    await page.getByRole('searchbox', { name: 'Search settings' }).fill('webhook revoke')
    await expect(page.getByRole('link', { name: /Webhook tokens/ })).toBeVisible()
    await page.getByRole('searchbox', { name: 'Search settings' }).press('Escape')
    await page.getByRole('button', { name: 'Create API key', exact: true }).click()
    const dialog = page.locator('.q-dialog')
    await expect(dialog.getByLabel('Expires in (days)')).toHaveValue('90')
    await dialog.getByLabel('Name', { exact: true }).fill('Report script')
    await dialog.getByRole('checkbox', { name: /workflows:run/ }).check()
    await dialog.getByRole('button', { name: 'Create API key', exact: true }).click()
    await expect(dialog.getByRole('alert')).toContainText('Choose at least one available resource')
    await dialog.getByRole('checkbox', { name: 'Grant workflows:run on Morning digest', exact: true }).check()
    await dialog.getByLabel('Expires in (days)').fill('7')
    await page.screenshot({ path: testInfo.outputPath(`api-key-grants-${width}.png`) })
    await dialog.getByRole('button', { name: 'Create API key', exact: true }).click()
    await expect(page.getByLabel('Secret key')).toHaveValue('one-time-api-secret')
    expect(state.creates[0].grants).toEqual([{ scope: 'workflows:run', resources: ['immutable-digest-id'] }])
    expect(state.creates[0].expires_at).toBeGreaterThan(Date.now() / 1000 + 6 * 86400)
    await page.getByRole('button', { name: 'Copy API key', exact: true }).click()
    await expect.poll(() => page.evaluate(() => window.copiedTokens)).toEqual(['one-time-api-secret'])
    const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }))
    expect(stored).not.toContain('one-time-api-secret')
    await page.getByRole('button', { name: 'Done, hide secret' }).click()
    await expect(page.getByLabel('Secret key')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'View Report script' })).toBeVisible()
    expect(await page.locator('.settings__body').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`api-key-inventory-${width}.png`) })
  })
}

test('create a key with an exact expiry date', async ({ page, context }) => {
  const state = await setup(page, context)
  await page.goto('/settings/api-keys')
  await page.getByRole('button', { name: 'Create API key', exact: true }).click()
  const dialog = page.locator('.api-keys__dialog')
  await dialog.getByLabel('Name', { exact: true }).fill('Short-lived maintenance')
  await dialog.getByRole('checkbox', { name: /profile:read/ }).check()
  await dialog.getByLabel('Expiry', { exact: true }).selectOption('date')
  const date = new Date(Date.now() + 2 * 86400000)
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
  await dialog.getByLabel('Expiry date and time').fill(local)
  await dialog.getByRole('button', { name: 'Create API key', exact: true }).click()
  await expect(page.getByLabel('Secret key')).toBeVisible()
  expect(Math.abs(state.creates[0].expires_at - date.getTime() / 1000)).toBeLessThan(60)
})

test('detail deep link, daily usage, pagination, rotation and revocation', async ({ page, context }, testInfo) => {
  const state = await setup(page, context)
  await page.goto('/settings/api-keys?token=existing#api-key-activity')
  await expect(page.getByRole('img', { name: /12 requests, 2 failures/ })).toBeVisible()
  await expect(page.getByText('trace-0', { exact: true })).toBeVisible()
  await page.getByText('Granted permissions (1)', { exact: true }).click()
  await expect(page.getByRole('region', { name: 'Key activity' }).getByText('Morning digest', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(page.getByText('trace-50', { exact: true })).toBeVisible()
  expect(state.offsets).toEqual([0, 50])
  await page.screenshot({ path: testInfo.outputPath('api-key-activity.png') })
  await page.getByRole('button', { name: 'Rotate', exact: true }).click()
  await page.getByRole('button', { name: 'Rotate key', exact: true }).click()
  await expect(page.getByLabel('Secret key')).toHaveValue('rotated-once-secret')
  await page.getByRole('button', { name: 'Done, hide secret' }).click()
  await expect(page.getByRole('button', { name: 'View Daily report replacement', exact: true })).toBeVisible()
  const row = page.locator('.api-keys__list > li').filter({ has: page.getByRole('button', { name: 'View Daily report replacement', exact: true }) })
  await row.getByRole('button', { name: 'Revoke', exact: true }).click()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  expect(state.revokes).toHaveLength(0)
  await row.getByRole('button', { name: 'Revoke', exact: true }).click()
  await page.getByRole('button', { name: 'Revoke key', exact: true }).click()
  await expect(row.getByText('Revoked', { exact: true })).toBeVisible()
  expect(state.revokes).toEqual(['/api/tokens/rotated'])
})

test('explicit all-resource grants, cancellation and rejected creation retain the draft', async ({ page, context }) => {
  const state = await setup(page, context)
  await page.goto('/settings/api-keys')
  await page.getByRole('button', { name: 'Create API key', exact: true }).click()
  const dialog = page.locator('.api-keys__dialog')
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  expect(state.creates).toHaveLength(0)
  await page.getByRole('button', { name: 'Create API key', exact: true }).click()
  await dialog.getByLabel('Name', { exact: true }).fill('Workspace integration')
  await dialog.getByRole('checkbox', { name: /workflows:run/ }).check()
  await dialog.getByLabel('Resource access for workflows:run', { exact: true }).selectOption({ label: 'All accessible resources' })
  await expect(dialog.getByText(/Includes resources you gain access to later/)).toBeVisible()
  await dialog.getByRole('checkbox', { name: /profile:read/ }).check()
  state.createFailure = true
  await dialog.getByRole('button', { name: 'Create API key', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('Permission no longer available')
  await expect(dialog.getByLabel('Name', { exact: true })).toHaveValue('Workspace integration')
  expect(state.creates[0].grants).toEqual([{ scope: 'workflows:run', resources: null }, { scope: 'profile:read', resources: null }])
  await expect(page.getByLabel('Secret key')).toHaveCount(0)
  state.createFailure = false
  await dialog.getByRole('button', { name: 'Create API key', exact: true }).click()
  await expect(page.getByLabel('Secret key')).toBeVisible()
})

test('webhook failure does not copy a URL and the shorter expiry is sent explicitly', async ({ page, context }) => {
  const state = await setup(page, context)
  state.webhookFailure = true
  await page.goto('/workflows/digest?tab=Run')
  await page.getByLabel('Link expires in').selectOption('1')
  await page.getByRole('button', { name: 'Copy webhook URL', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Workflow access removed')
  expect(state.webhooks).toEqual([{ days: 1 }])
  expect(await page.evaluate(() => window.copiedTokens)).toEqual([])
  await expect(page.getByLabel('Webhook URL', { exact: true })).toHaveCount(0)
})

test('history failure is recoverable and does not hide successful aggregates', async ({ page, context }) => {
  const state = await setup(page, context)
  state.requestFailure = true
  await page.goto('/settings/api-keys?token=existing')
  await expect(page.getByRole('alert')).toContainText('Request history unavailable')
  await expect(page.getByRole('img', { name: /12 requests, 2 failures/ })).toBeVisible()
  state.requestFailure = false
  await page.getByRole('button', { name: 'Retry activity', exact: true }).click()
  await expect(page.getByText('trace-0', { exact: true })).toBeVisible()
})

for (const clipboardFails of [false, true]) {
  test(`webhook copying mints exact-workflow credentials first, clipboard failure=${clipboardFails}`, async ({ page, context }) => {
    const state = await setup(page, context, { clipboardFails })
    await page.goto('/workflows/digest?tab=Run')
    await expect(page.getByLabel('Link expires in')).toHaveValue('7')
    await page.getByRole('button', { name: 'Copy webhook URL', exact: true }).click()
    await expect(page.getByLabel('Webhook URL', { exact: true })).toHaveValue('https://example.test/api/triggers/digest?token=webhook-once')
    expect(state.webhooks).toEqual([{ days: 7 }])
    if (clipboardFails) {
      await page.locator('.token-secret').getByRole('button', { name: 'Copy webhook URL', exact: true }).click()
      await expect(page.locator('.token-secret').getByRole('alert')).toContainText('copy it manually')
      expect(state.webhooks).toHaveLength(1)
    } else await expect.poll(() => page.evaluate(() => window.copiedTokens)).toEqual(['https://example.test/api/triggers/digest?token=webhook-once'])
    expect(await page.evaluate(() => JSON.stringify({ ...localStorage }))).not.toContain('webhook-once')
    await page.locator('.token-secret').getByRole('link', { name: 'Manage or revoke this token' }).click()
    await expect(page).toHaveURL(/\/settings\/api-keys\?token=webhook/)
    await expect(page.getByLabel('Webhook URL', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'Key activity' })).toContainText('Morning digest webhook')
  })
}
