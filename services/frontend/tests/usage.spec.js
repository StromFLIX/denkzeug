import { test, expect } from './account-fixture.js'

const tokens = { input: 3600000, output: 1200000, cacheRead: 18400000, cacheWrite: 1600000, total: 24800000 }
const facets = { models: [{ id: 'copilot/gpt-6.1-sol', label: 'copilot/gpt-6.1-sol' }, { id: 'anthropic/sonnet', label: 'anthropic/sonnet' }], agents: [{ id: 'a1', label: 'Digest writer' }], workflows: [{ id: 'hn_daily_digest', label: 'hn_daily_digest' }], users: [{ id: 'test-admin', label: 'Alex' }] }
const total = { tokens, responses: 6320, reports: 6040, executions: 746 }
const chartOptions = {
  models: facets.models,
  agents: facets.agents,
  workload: [{ id: 'chats', label: 'Chats' }, { id: 'workflows', label: 'Workflows' }],
  workflows: [{ id: '', label: 'No workflow' }, ...facets.workflows],
  users: facets.users
}
function payload (params) {
  const group = params.get('group') || 'models'
  const chartGroup = params.get('chart_group') || 'models'
  const start = params.get('start') || '2026-11-01', end = params.get('end') || '2026-11-30'
  const first = Date.parse(start), count = (Date.parse(end) - first) / 86400000 + 1
  const options = chartOptions[chartGroup]
  const daily = Array.from({ length: count }, (_, i) => {
    const date = new Date(first + i * 86400000).toISOString().slice(0, 10)
    const volume = i === 0 ? 1600000 : 800000
    return { date, tokens: { total: volume }, executions: 25, series: options.length === 2 ? [volume * .75, volume * .25] : [volume] }
  })
  const series = options.map((item, i) => ({ ...item, total: daily.reduce((sum, day) => sum + day.series[i], 0) }))
  const summary = { ...total, tokens: { ...tokens, total: series.reduce((sum, item) => sum + item.total, 0) } }
  const rows = ['executions', 'runs'].includes(group) ? [{ id: 'chat:t1', label: 'Digest writer', agent_name: 'Digest writer', agent_set: 'default', model: 'copilot/gpt-6.1-sol', created_at: 1794744000, workflow: 'hn_daily_digest', trigger: 'schedule', state: 'completed', href: group === 'runs' ? '/runs/digest-run' : '/chats/task', workload: 'workflows', ...total }] : group === 'workflows' ? [{ id: 'hn_daily_digest', label: 'hn_daily_digest', ...total }] : group === 'agents' ? [{ id: 'a1', label: 'Digest writer', ...total }] : group === 'users' ? [{ id: 'test-admin', label: 'Alex', ...total }] : [{ id: 'copilot/gpt-6.1-sol', label: 'copilot/gpt-6.1-sol', ...total }]
  return structuredClone({ start, end, timezone: 'UTC', admin: true, facets, summary, daily, chart: { group: chartGroup, series }, group, rows, count: rows.length, offset: 0, limit: 50 })
}
async function mock (page, handler) {
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/api/auth/')) return route.fallback()
    if (url.pathname === '/api/settings/usage/export') return route.fulfill({ contentType: 'text/csv', body: 'model,total\ncopilot/gpt-6.1-sol,24800000', headers: { 'Content-Disposition': 'attachment; filename="denkzeug-usage.csv"' } })
    if (url.pathname === '/api/settings/usage') return handler ? handler(route, url) : route.fulfill({ json: payload(url.searchParams) })
    if (url.pathname === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: ': connected\n\n' })
    return route.fulfill({ json: url.pathname === '/api/catalog' ? { models: [], tools: [], agents: [] } : { chats: [], drafts: [], workflows: [], runs: [] } })
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
  const range = page.getByRole('region', { name: 'Selected range token breakdown' })
  const grouping = page.getByRole('combobox', { name: 'Group by' })
  const chart = page.getByRole('region', { name: 'Token consumption over time' })
  await expect(grouping).toHaveValue('models')
  await expect(range).toContainText('anthropic/sonnet')
  await chart.getByRole('button', { name: /Nov 1, 2026:/ }).click()
  await expect(chart.getByRole('status')).toContainText('copilot/gpt-6.1-sol 1.20M')
  const mint = await range.locator('i').first().evaluate(el => getComputedStyle(el).backgroundColor)
  expect(await chart.getByRole('button', { pressed: true }).locator('span').first().evaluate(el => getComputedStyle(el).backgroundColor)).toBe(mint)
  for (const [value, label] of [['users', 'Alex'], ['agents', 'Digest writer'], ['workflows', 'No workflow'], ['workload', 'Chats']]) {
    await grouping.selectOption(value)
    await expect(range).toContainText(label)
    await expect(chart.getByRole('status')).toContainText(label)
    await expect(chart.getByRole('button', { pressed: true })).toHaveAttribute('data-date', '2026-11-01')
  }
  await expect(page).toHaveURL(/chart_group=workload/)
  await expect(page.getByRole('tab', { name: 'Models', exact: true })).toHaveAttribute('aria-selected', 'true')
  await grouping.selectOption('models')
  await expect(range).toContainText('anthropic/sonnet')
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
  await expect.poll(() => new URL(page.url()).searchParams.get('group')).toBe('workflows')
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
  const toggle = page.getByRole('button', { name: /^Filters/ })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('combobox', { name: 'Model', exact: true })).toBeHidden()
  await toggle.click()
  await expect(page.getByRole('combobox', { name: 'Model', exact: true })).toBeVisible()
  await page.getByRole('combobox', { name: 'Usage breakdown', exact: true }).selectOption('models')
  await expect(page.locator('.usage__total')).toHaveText('24.80M')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await toggle.click()
  const picker = page.getByRole('combobox', { name: 'Select a day to inspect' })
  const chart = page.getByRole('region', { name: 'Token consumption over time' })
  await picker.selectOption('2026-11-20')
  await expect(chart.getByRole('status')).toContainText('Nov 20')
  await expect(chart.getByRole('button', { pressed: true })).toHaveAttribute('data-date', '2026-11-20')
  await chart.getByRole('button', { name: 'Clear day' }).click()
  await expect(picker).toHaveValue('')
  await expect(picker).toBeFocused()
  await chart.getByRole('button', { name: /Nov 21, 2026:/ }).click()
  await expect(picker).toHaveValue('2026-11-21')
  const sideBySide = await page.locator('.usage__hero').evaluate(el => {
    const total = el.querySelector('.usage__total').getBoundingClientRect()
    const types = el.querySelector('.usage__tokens').getBoundingClientRect()
    return total.right < types.left && total.top < types.bottom && types.top < total.bottom
  })
  expect(sideBySide).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('usage-mobile.png'), fullPage: true })
  await page.getByRole('combobox', { name: 'Usage breakdown', exact: true }).selectOption('workflows')
  await page.getByRole('button', { name: 'Explore hn_daily_digest', exact: true }).click()
  await expect(page.getByRole('combobox', { name: 'Usage breakdown', exact: true })).toHaveValue('runs')
  await page.getByRole('button', { name: 'Clear workflow filter', exact: true }).click()
  await expect(page.getByRole('combobox', { name: 'Usage breakdown', exact: true })).toHaveValue('workflows')
  await page.evaluate(() => localStorage.setItem('nautionette.preferences.v1', JSON.stringify({ theme: 'orbit' })))
  await page.reload()
  await expect(page.locator('.usage__total')).toHaveText('24.80M')
  await expect(page.locator('html')).toHaveAttribute('data-color-scheme', 'dark')
  await expect(page.locator('.range__legend i').first()).toHaveCSS('background-color', 'rgb(121, 222, 196)')
})

test('long ranges stay daily and selecting a date scrolls only within the chart', async ({ page }) => {
  await mock(page)
  await page.goto('/settings/usage?period=custom&start=2025-11-30&end=2026-11-30&chart_group=agents')
  await expect(page.getByRole('combobox', { name: 'Group by' })).toHaveValue('agents')
  await expect(page.getByRole('list', { name: 'Tokens by date' }).getByRole('listitem')).toHaveCount(366)
  const picker = page.getByRole('combobox', { name: 'Select a day to inspect' })
  await picker.selectOption('2026-11-29')
  await expect(page.locator('.chart__detail')).toContainText('Nov 29, 2026')
  await expect.poll(() => page.locator('.chart__scroll').evaluate(el => el.scrollLeft)).toBeGreaterThan(0)
  await page.locator('.chart__bar[aria-pressed=true]').focus()
  await page.keyboard.press('ArrowRight')
  await expect(picker).toHaveValue('2026-11-30')
  await page.keyboard.press('Home')
  await expect(picker).toHaveValue('2025-11-30')
  await expect.poll(() => page.locator('.chart__scroll').evaluate(el => el.scrollLeft)).toBe(0)
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
  await page.goto(`${path}&chart_group=users`)
  await expect(page.getByRole('heading', { name: 'Usage', exact: true })).toBeVisible()
  const grouping = page.getByRole('combobox', { name: 'Group by' })
  await expect(grouping).toHaveValue('models')
  await expect(grouping.locator('option[value=users]')).toHaveCount(0)
  await expect(page.getByRole('tab', { name: 'Users', exact: true })).toHaveCount(0)
  await expect(page.getByRole('combobox', { name: 'Account owner', exact: true })).toHaveCount(0)
})
