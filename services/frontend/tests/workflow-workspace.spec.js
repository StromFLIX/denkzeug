import { expect, test } from './account-fixture.js'

async function workspace (page) {
  const state = { started: null, searches: [] }
  const workflow = { name: 'weekly_summary', title: 'Weekly summary', description: 'Collect product updates.', settings: {}, runs: [], schedule: null, code: 'MANIFEST = {}', graph: { nodes: [], edges: [] }, manifest: { inputs: { type: 'object', properties: {
    mode: { title: 'Report type', type: 'string', enum: ['weekly', 'release'], x_ui: { enum_labels: ['Weekly summary', 'Release notes'] } },
    agent: { title: 'Agent', type: 'string', x_resource: { kind: 'agent' } },
    zero: { title: 'Look back', type: 'integer' }, empty: { title: 'Notes', type: 'string' }, nil: { title: 'Destination', type: ['string', 'null'] }
  }, required: ['mode', 'agent'] } } }
  const agents = [{ value: 'release-a', label: 'Release assistant', description: 'Automation' }, { value: 'release-b', label: 'Release assistant', description: 'Docs team' }]
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname
    if (path.startsWith('/api/auth/')) return route.fallback()
    if (path === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: ': connected\n\n' })
    let data = {}
    if (path === '/api/catalog') data = { models: [], tools: [], agents: [], agent_sets: [] }
    if (path === '/api/system') data = { components: [] }
    if (path === '/api/chats') data = { chats: [] }
    if (path === '/api/drafts') data = { drafts: [] }
    if (path === '/api/workflows') data = { workflows: [workflow] }
    if (path === '/api/workflows/weekly_summary') data = workflow
    if (path.endsWith('/input-options')) {
      state.searches.push(url.searchParams.get('q'))
      const selected = agents.find(item => item.value === url.searchParams.get('selected')) || null
      data = { items: agents.filter(item => item.label.toLowerCase().includes((url.searchParams.get('q') || '').toLowerCase())), selected, next_offset: null }
    }
    if (path.endsWith('/run')) { state.started = route.request().postDataJSON(); data = { workflow_id: 'new-run' } }
    if (path === '/api/runs') data = { runs: [], next_cursor: '' }
    if (path === '/api/runs/new-run') data = { run: { workflow: 'weekly_summary', workflow_id: 'new-run', status: 'running', input: state.started?.input }, temporal: { status: 'RUNNING' } }
    return route.fulfill({ json: data })
  })
  await page.goto('/workflows/weekly_summary')
  return state
}

test('human typed inputs, enums and searchable names submit stable references', async ({ page }) => {
  const state = await workspace(page)
  await expect(page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Runs', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await page.getByLabel('Report type', { exact: true }).selectOption({ label: 'Release notes' })
  const agent = page.getByLabel('Agent', { exact: true })
  await agent.fill('release')
  await page.getByRole('option').filter({ hasText: 'Docs team' }).click()
  await expect.poll(() => state.searches.length).toBeGreaterThan(0)
  await page.getByLabel('Look back', { exact: true }).fill('0')
  await page.getByLabel('Notes value mode').selectOption('value')
  await page.getByLabel('Notes', { exact: true }).fill('')
  await page.getByLabel('Destination value mode').selectOption('null')
  await page.getByRole('button', { name: 'Run now', exact: true }).click()
  await expect.poll(() => state.started).toEqual({ input: { mode: 'release', agent: 'release-b', zero: 0, empty: '', nil: null } })
  await expect(page).toHaveURL(/\/workflows\/weekly_summary\/runs\/new-run$/)
  await expect(page.getByRole('button', { name: 'Details', exact: true })).toHaveClass(/tab--active/)
  await expect(page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Workflows', exact: true })).toHaveAttribute('aria-current', 'page')
})

test('field errors preserve the draft and missing inputs do not start execution', async ({ page }) => {
  const state = await workspace(page)
  await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
  await page.getByRole('button', { name: 'Run now', exact: true }).click()
  await expect(page.getByText('Supply a value for this required input.')).toHaveCount(2)
  expect(state.started).toBeNull()
  await page.getByLabel('Report type', { exact: true }).selectOption({ label: 'Weekly summary' })
  await expect(page.getByLabel('Report type', { exact: true })).toHaveValue('choice-0')
  await page.getByRole('button', { name: 'Close run form' }).click()
  await expect(page.getByText('Discard run inputs?', { exact: true })).toBeVisible()
})

for (const width of [320, 390, 1440]) {
  test(`overview, manual form and all runs fit at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await workspace(page)
    expect(await page.locator('.shell__main').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
    await page.getByRole('button', { name: 'Run workflow', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Run Weekly summary', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.getByRole('button', { name: 'Run now', exact: true }).click({ trial: true })
    await page.screenshot({ path: test.info().outputPath('manual-run.png') })
    await page.getByRole('button', { name: 'Close run form' }).click()
    await page.goto('/workflows?view=runs')
    await expect(page.getByRole('heading', { name: 'All runs', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}
