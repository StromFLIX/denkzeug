import { expect, test, fixtureAccount } from './account-fixture.js'
import { mockDesign } from './design-fixture.js'

async function setup (page, context, { member = false, empty = false, clipboardFails = false } = {}) {
  const design = await mockDesign(context)
  if (member) await page.route('**/api/auth/me', route => route.fulfill({ json: { ...fixtureAccount, user: { ...fixtureAccount.user, role: 'member' } } }))
  await context.addInitScript(fails => {
    window.copiedTokens = []
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { if (fails) throw new Error('Clipboard denied'); window.copiedTokens.push(text) } } })
    if (fails) document.execCommand = () => false
  }, clipboardFails)
  const now = Math.floor(Date.now() / 1000)
  const catalog = {
    scopes: [{ scope: 'workflows:run', description: 'Run a workflow', resource_kind: 'workflow' }, { scope: 'profile:read', description: 'Read your profile', resource_kind: null }],
    policy: { default_days: 90, max_days: 365, webhook_days: 7, allow_never_expire: true },
    resources: [{ kind: 'workflow', id: 'immutable-digest-id', name: 'Morning digest' }]
  }
  const token = { id: 'existing', name: 'Daily report', prefix: 'dz_report', kind: 'api_key', grants: [{ scope: 'workflows:run', resources: ['immutable-digest-id'] }], created_at: now - 86400, expires_at: now + 86400 * 90, revoked_at: null, last_used_at: now, requests_7d: 123, failures_7d: 2 }
  const state = { workflow: design.workflow, tokens: empty ? [] : [token], creates: [], rotations: [], revokes: [], offsets: [], webhooks: [], requestFailure: false, createFailure: false, webhookFailure: false }
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
    const body = route.request().postDataJSON()
    state.webhooks.push(body)
    if (state.webhookFailure) return route.fulfill({ status: 403, json: { detail: 'Workflow access removed' } })
    const webhook = { ...token, id: 'webhook', name: 'Morning digest webhook', kind: 'webhook', expires_at: body.days === null ? null : now + body.days * 86400 }
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

test('detail deep link, daily usage, pagination, never-expiring rotation and revocation', async ({ page, context }, testInfo) => {
  const state = await setup(page, context)
  state.tokens[0].expires_at = null
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
  await expect(page.locator('.token-secret')).toContainText('Never expires')
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
  await page.goto('/workflows/digest?tab=Integrate')
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

for (const width of [1440, 390, 320]) {
  test(`members create never-expiring keys with unchanged permissions at ${width}px`, async ({ page, context }) => {
    const state = await setup(page, context, { member: true })
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/settings/api-keys')
    await page.getByRole('button', { name: 'Create API key', exact: true }).click()
    const dialog = page.locator('.api-keys__dialog')
    await dialog.getByLabel('Name', { exact: true }).fill('Permanent integration')
    await dialog.getByRole('checkbox', { name: /profile:read/ }).check()
    await dialog.getByLabel('Expiry', { exact: true }).selectOption('never')
    await expect(dialog.getByText(/Revoke it manually/)).toBeVisible()
    await expect(dialog.getByLabel('Expiry date and time')).toHaveCount(0)
    await expect(dialog.getByLabel('Expires in (days)')).toHaveCount(0)
    await dialog.getByRole('button', { name: 'Create API key', exact: true }).click()
    await expect(page.getByLabel('Secret key')).toBeVisible()
    expect(state.creates[0]).toEqual({ name: 'Permanent integration', expires_at: null, grants: [{ scope: 'profile:read', resources: null }] })
    await expect(page.locator('.token-secret')).toContainText('Never expires')
    await expect(page.locator('.token-secret')).toContainText('Revoke it manually')
    await page.getByRole('button', { name: 'Done, hide secret' }).click()
    const row = page.locator('.api-keys__list > li').filter({ has: page.getByRole('button', { name: 'View Permanent integration', exact: true }) })
    await expect(row).toContainText('Never expires')
    await expect(row).toContainText('Never used')
    await expect(row.getByText('Active', { exact: true })).toBeVisible()
    await expect(row.getByRole('button', { name: 'Rotate', exact: true })).toBeVisible()
    await expect(row.getByRole('button', { name: 'Revoke', exact: true })).toBeVisible()
    expect(await page.locator('.settings__body').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  })

  test(`all four integration formats mint never-expiring exact-workflow tokens at ${width}px`, async ({ page, context }, testInfo) => {
    const state = await setup(page, context, { member: true })
    const inputs = { message: "O'Reilly & 東京", enabled: true, optional: null, nested: { list: [1, false] } }
    state.workflow.manifest.inputs = { type: 'object', properties: Object.fromEntries(Object.keys(inputs).map(key => [key, { default: inputs[key] }])) }
    state.workflow.schedules = [{ id: 'schedule-digest', description: 'Every day at 08:00', timezone: 'UTC', input: inputs }]
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/workflows/digest?tab=Integrate')
    await page.getByLabel('Example inputs').selectOption('schedule-digest')
    const trigger = page.locator('.trigger')
    await expect(trigger.getByRole('button', { name: 'curl', exact: true })).toHaveAttribute('aria-pressed', 'true')
    expect(state.webhooks).toHaveLength(0)
    await trigger.getByLabel('Link expires in').selectOption({ label: 'Never expires' })
    await expect(trigger.getByText(/Revoke it manually/)).toBeVisible()
    for (const format of ['curl', 'python', 'node', 'GET']) {
      await trigger.getByRole('button', { name: format, exact: true }).click()
      const preview = trigger.getByLabel('Integration preview')
      await expect(preview).toContainText('YOUR_WORKFLOW_TOKEN')
      await expect(preview).not.toContainText('webhook-once')
      if (format === 'curl') await expect(preview).toContainText('curl -X POST')
      if (format === 'python') await expect(preview).toContainText('httpx.post(')
      if (format === 'node') await expect(preview).toContainText('method: "POST"')
      if (format === 'GET') expect(JSON.parse(new URL(await preview.innerText()).searchParams.get('input'))).toEqual(inputs)
      await trigger.getByRole('button', { name: 'Copy', exact: true }).click()
      await expect(page.getByLabel('Integration snippet', { exact: true })).toBeVisible()
      const text = await page.getByLabel('Integration snippet', { exact: true }).inputValue()
      expect(text).toContain('webhook-once')
      expect(text).not.toContain('YOUR_WORKFLOW_TOKEN')
      expect(text).not.toMatch(/Authorization|Bearer/)
      if (format === 'GET') expect(JSON.parse(new URL(text).searchParams.get('input'))).toEqual(inputs)
      expect(state.webhooks.at(-1)).toEqual({ days: null })
      await expect(page.locator('.token-secret')).toContainText('Never expires')
      await expect.poll(() => page.evaluate(() => window.copiedTokens.at(-1))).toEqual(text)
      expect(await page.locator('.token-secret').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`integration-${format}-${width}.png`), animations: 'disabled' })
      await page.getByRole('button', { name: 'Done, hide secret' }).click()
      await expect(page.getByLabel('Integration snippet', { exact: true })).toHaveCount(0)
      await expect(preview).toContainText('YOUR_WORKFLOW_TOKEN')
    }
    await page.screenshot({ path: testInfo.outputPath(`integration-preview-${width}.png`), animations: 'disabled' })
    expect(state.webhooks).toHaveLength(4)
    expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('webhook-once')
    expect(await trigger.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await trigger.getByRole('button', { name: 'Copy', exact: true }).click()
    await expect(page.getByLabel('Integration snippet', { exact: true })).toBeVisible()
    await page.locator('.token-secret').getByRole('link', { name: 'Manage or revoke this token' }).click()
    await expect(page.getByLabel('Integration snippet', { exact: true })).toHaveCount(0)
  })
}

test('integration clipboard failure can be retried without issuing another credential', async ({ page, context }) => {
  const state = await setup(page, context, { clipboardFails: true })
  await page.goto('/workflows/digest?tab=Integrate')
  await page.locator('.trigger').getByRole('button', { name: 'Copy', exact: true }).click()
  await expect(page.getByLabel('Integration snippet', { exact: true })).toBeVisible()
  await page.locator('.token-secret').getByRole('button', { name: 'Copy integration snippet', exact: true }).click()
  await expect(page.locator('.token-secret').getByRole('alert')).toContainText('copy it manually')
  expect(state.webhooks).toEqual([{ days: 7 }])
  await page.getByRole('button', { name: 'Done, hide secret' }).click()
  await expect(page.getByLabel('Integration snippet', { exact: true })).toHaveCount(0)
  await expect(page.getByLabel('Integration preview')).toContainText('YOUR_WORKFLOW_TOKEN')
})

test('leaving Integrate during token creation never reveals or copies the stale secret', async ({ page, context }) => {
  await setup(page, context)
  let release
  const paused = new Promise(resolve => { release = resolve })
  let requested = false
  await page.route('**/api/workflows/digest/webhook-token', async route => {
    requested = true
    await paused
    await route.fulfill({ json: { token: { id: 'stale', expires_at: null }, secret: 'stale-secret', url: 'https://example.test/api/triggers/digest?token=stale-secret', expires_at: null } })
  })
  await page.goto('/workflows/digest?tab=Integrate')
  await page.locator('.trigger').getByRole('button', { name: 'Copy', exact: true }).click()
  await expect.poll(() => requested).toBe(true)
  await page.getByRole('button', { name: 'Overview', exact: true }).click()
  await expect(page.locator('.trigger')).toHaveCount(0)
  release()
  // Let the response reach the now-unmounted component, then return to a new preview.
  await page.getByRole('button', { name: 'Integrate', exact: true }).click()
  await expect(page.getByLabel('Integration preview')).toContainText('YOUR_WORKFLOW_TOKEN')
  await expect(page.getByLabel('Integration snippet', { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => window.copiedTokens)).toEqual([])
})

for (const clipboardFails of [false, true]) {
  test(`webhook copying mints exact-workflow credentials first, clipboard failure=${clipboardFails}`, async ({ page, context }) => {
    const state = await setup(page, context, { clipboardFails })
    await page.goto('/workflows/digest?tab=Integrate')
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
