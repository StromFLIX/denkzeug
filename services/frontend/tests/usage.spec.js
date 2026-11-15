import { test, expect } from './account-fixture.js'

const tokens = { input: 3600000, output: 1200000, cacheRead: 18400000, cacheWrite: 1600000, total: 24800000 }
const facets = { models: [{ id: 'copilot/gpt-6.1-sol', label: 'copilot/gpt-6.1-sol' }, { id: 'anthropic/sonnet', label: 'anthropic/sonnet' }], agents: [{ id: 'a1', label: 'Digest writer' }], workflows: [{ id: 'hn_daily_digest', label: 'hn_daily_digest' }], users: [{ id: 'test-admin', label: 'Alex' }] }
const total = { tokens, responses: 6320, reports: 6040, executions: 746 }
const daily = Array.from({ length: 30 }, (_, i) => ({ date: `2026-11-${String(i + 1).padStart(2, '0')}`, tokens: { total: 800000 }, chats: { total: 600000 }, workflows: { total: 200000 }, executions: 25 }))
function payload (params) {
  const group = params.get('group') || 'models'
  const rows = ['executions', 'runs'].includes(group) ? [{ id: 'chat:t1', label: 'Digest writer', agent_name: 'Digest writer', agent_set: 'default', model: 'copilot/gpt-6.1-sol', created_at: 1794744000, workflow: 'hn_daily_digest', trigger: 'schedule', state: 'completed', href: group === 'runs' ? '/runs/digest-run' : '/chats/task', workload: 'workflows', ...total }] : group === 'workflows' ? [{ id: 'hn_daily_digest', label: 'hn_daily_digest', ...total }] : group === 'agents' ? [{ id: 'a1', label: 'Digest writer', ...total }] : group === 'users' ? [{ id: 'test-admin', label: 'Alex', ...total }] : [{ id: 'copilot/gpt-6.1-sol', label: 'copilot/gpt-6.1-sol', ...total }]
  return structuredClone({ start: '2026-11-01', end: '2026-11-30', timezone: 'UTC', admin: true, facets, summary: total, daily, group, rows, count: rows.length, offset: 0, limit: 50 })
}
async function mock (page, handler) {
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/api/auth/')) return route.fallback()
    if (url.pathname === '/api/settings/usage/export') return route.fulfill({ contentType: 'text/csv', body: 'model,total\ncopilot/gpt-6.1-sol,24800000', headers: { 'Content-Disposition': 'attachment; filename="denkzeug-usage.csv"' } })
    if (url.pathname === '/api/settings/usage') return handler ? handler(route, url) : route.fulfill({ json: payload(url.searchParams) })
    if (url.pathname === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: ': connected\n\n' })
    return route.fulfill({ json: url.pathname === '/api/catalog' ? { models: [], tools: [], agents: [] } : { chats: [], drafts: [], workflows: [], runs: [], apps: [] } })
  })
}
const path = '/settings/usage?period=custom&start=2026-11-01&end=2026-11-30'

test('overview renders approved chart and composition without coverage panels', async ({ page }, testInfo) => {
  await mock(page)
  await page.goto(path)
  await expect(page.getByRole('heading', { name: 'Usage', exact: true })).toBeVisible()
  await expect(page.locator('.usage__total')).toHaveText('24.80M')
  await expect(page.locator('.usage__tokens')).toContainText('18.40M')
  await expect(page.getByRole('list', { name: 'Tokens by date' }).getByRole('listitem')).toHaveCount(30)
  await page.locator('.usage__bar').first().focus()
  await expect(page.locator('.usage__chart-detail')).toContainText('Chats 600,000')
  await expect(page.getByText('View data coverage')).toHaveCount(0)
  await expect(page.getByText('Reported usage is a lower bound')).toHaveCount(0)
  await page.locator('.settings__body').evaluate(el => { el.scrollTop = 0 })
  await page.screenshot({ path: testInfo.outputPath('usage-desktop.png'), fullPage: true })
})

test('breakdowns, workflow drilldown, query filters and CSV export work', async ({ page }) => {
  await mock(page)
  await page.goto(path)
  await page.getByRole('tab', { name: 'Workflows', exact: true }).click()
  await page.getByRole('button', { name: 'Explore hn_daily_digest', exact: true }).click()
  await expect(page).toHaveURL(/workflow=hn_daily_digest/)
  await expect(page.getByRole('tab', { name: 'Workflows', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByText('Runs in hn_daily_digest', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Open execution chat:t1' })).toHaveAttribute('href', '/runs/digest-run')
  await page.getByRole('button', { name: 'Reset filters', exact: true }).click()
  await expect(page).not.toHaveURL(/workflow=/)
  await page.getByRole('tab', { name: 'Models', exact: true }).click()
  await page.getByRole('combobox', { name: 'Model', exact: true }).selectOption('anthropic/sonnet')
  await expect.poll(() => new URL(page.url()).searchParams.get('model')).toBe('anthropic/sonnet')
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export CSV', exact: true }).click()
  expect((await download).suggestedFilename()).toBe('denkzeug-usage.csv')
})

test('keyboard tabs, mobile filters and both themes remain usable', async ({ page }, testInfo) => {
  await mock(page)
  await page.goto(path)
  await page.getByRole('tab', { name: 'Models', exact: true }).focus()
  await page.keyboard.press('ArrowRight')
  await expect(page.getByRole('tab', { name: 'Agents', exact: true })).toHaveAttribute('aria-selected', 'true')
  await page.setViewportSize({ width: 320, height: 844 })
  await page.getByRole('button', { name: /^Filters/ }).click()
  await expect(page.getByRole('combobox', { name: 'Model', exact: true })).toBeVisible()
  await page.getByRole('combobox', { name: 'Usage breakdown', exact: true }).selectOption('models')
  await expect(page.locator('.usage__total')).toHaveText('24.80M')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('button', { name: /^Filters/ }).click()
  await page.screenshot({ path: testInfo.outputPath('usage-mobile.png'), fullPage: true })
  await page.evaluate(() => localStorage.setItem('nautionette.preferences.v1', JSON.stringify({ theme: 'orbit' })))
  await page.reload()
  await expect(page.locator('.usage__total')).toHaveText('24.80M')
})

test('unknown counts are dashes, empty states and retry recover without fabricated zeros', async ({ page }) => {
  let state = 'unknown'
  await mock(page, (route, url) => {
    if (state === 'error') return route.fulfill({ status: 503, json: { detail: 'Usage temporarily unavailable' } })
    const data = payload(url.searchParams)
    if (state === 'unknown') { data.summary.tokens = null; data.rows[0].tokens = null }
    if (state === 'empty') { data.summary = { tokens: null, executions: 0, responses: 0, reports: 0 }; data.rows = []; data.count = 0 }
    return route.fulfill({ json: data })
  })
  await page.goto(path)
  await expect(page.locator('.usage__total')).toHaveText('—')
  await expect(page.getByText('These executions did not report token counts.')).toBeVisible()
  state = 'error'
  await page.getByRole('button', { name: 'Refresh usage', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Usage temporarily unavailable')
  state = 'empty'
  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByText('No agent executions in this period. Try a wider date range.')).toBeVisible()
})

test('member settings exposes Usage without workspace user controls', async ({ page }) => {
  await mock(page)
  await page.route('**/api/auth/me', route => route.fulfill({ json: { user: { id: 'member', username: 'member', display_name: 'Member', role: 'member', status: 'active' }, csrf_token: 'csrf' } }))
  await page.goto(path)
  await expect(page.getByRole('heading', { name: 'Usage', exact: true })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Users', exact: true })).toHaveCount(0)
  await expect(page.getByRole('combobox', { name: 'Account owner', exact: true })).toHaveCount(0)
})
