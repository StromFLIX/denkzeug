import { test, expect, fixtureAccount } from './account-fixture.js'
import { mockAgents } from './agent-fixture.js'

const initial = { id: 'report', title: 'Report', description: 'Weekly totals', html: '<!doctype html><h1>Original report</h1>', input_schema: { type: 'object' }, sample_data: { count: 4 }, change_note: 'Initial report', revision: 1 }
async function mockLibrary (context, seed = [initial]) {
  await mockAgents(context)
  const state = { records: new Map(seed.map(item => [item.id, { archived: false, revisions: [structuredClone(item)] }])), writes: [], reads: [], fail: '', reloadFail: false }
  const summary = (id, app) => ({ id, title: app.revisions.at(-1).title, description: app.revisions.at(-1).description, active_revision: app.revisions.at(-1).revision, archived: app.archived, updated_at: 1700000000 })
  await context.route('**/api/apps{,/**}', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    state.reads.push(path)
    if (path === '/api/apps' && request.method() === 'GET') return route.fulfill({ json: { apps: [...state.records].map(([id, app]) => summary(id, app)) } })
    if (request.method() === 'POST') {
      const body = request.postDataJSON(); state.writes.push(body)
      if (state.fail) return route.fulfill({ status: 409, json: { detail: state.fail } })
      const app = state.records.get(body.id)
      if (body.expected_revision !== (app?.revisions.at(-1).revision || 0)) return route.fulfill({ status: 409, json: { detail: 'Revision conflict; refresh history before publishing.' } })
      const { expected_revision, ...content } = body
      const revision = { ...content, revision: expected_revision + 1 }
      if (app) app.revisions.push(revision)
      else state.records.set(body.id, { archived: false, revisions: [revision] })
      return route.fulfill({ json: revision })
    }
    const [, , , id, , number] = path.split('/'), app = state.records.get(id)
    if (!app) return route.fulfill({ status: 404, json: { detail: 'App not found' } })
    if (request.method() === 'PATCH') {
      const body = request.postDataJSON(); state.writes.push({ id, ...body })
      app.archived = body.archived
      return route.fulfill({ json: { app: summary(id, app) } })
    }
    if (number) return route.fulfill({ json: app.revisions.find(item => item.revision === Number(number)) })
    if (state.reloadFail) return route.fulfill({ status: 503, json: { detail: 'Source temporarily unavailable' } })
    return route.fulfill({ json: { app: summary(id, app), revisions: app.revisions.map(({ revision, change_note }) => ({ revision, change_note })), revision: app.revisions.at(-1) } })
  })
  return state
}

for (const width of [1440, 360]) {
  test(`create, inspect and publish a new app without running workflows at ${width}px`, async ({ page, context }) => {
    const state = await mockLibrary(context, [])
    const workflowRuns = []
    page.on('request', request => { if (/\/api\/workflows\/.*\/run/.test(request.url())) workflowRuns.push(request.url()) })
    await page.setViewportSize({ width, height: 1000 })
    await page.goto('/settings/apps')
    await expect(page.getByText('No apps yet.', { exact: false })).toBeVisible()
    await page.getByRole('button', { name: 'Create app', exact: true }).click()
    await page.getByLabel('Title', { exact: true }).fill('Counter')
    await page.getByLabel('Slug', { exact: true }).fill('counter')
    await page.getByLabel('Self-contained HTML', { exact: true }).fill('<!doctype html><p>Counter</p>')
    await page.getByLabel('Sample data (JSON object)', { exact: true }).fill('{"count":3}')
    await page.getByLabel('Change note', { exact: true }).fill('First version')
    await expect(page.getByRole('status').filter({ hasText: 'Unsaved draft' })).toBeVisible()
    expect(state.writes).toHaveLength(0)
    await page.getByRole('button', { name: 'Preview sample', exact: true }).click()
    await expect(page.getByRole('region', { name: 'Sample preview', exact: true })).toBeVisible()
    expect(state.writes).toHaveLength(0)
    await page.getByRole('button', { name: 'Publish new immutable revision', exact: true }).click()
    await expect(page.getByRole('status').filter({ hasText: 'Published revision 1.' })).toBeVisible()
    expect(state.writes[0]).toMatchObject({ id: 'counter', expected_revision: 0, sample_data: { count: 3 }, change_note: 'First version' })
    expect(workflowRuns).toEqual([])
    await expect(page.getByLabel('Slug', { exact: true })).toHaveAttribute('readonly', '')
    expect(await page.locator('.settings__body').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await page.screenshot({ path: test.info().outputPath(`apps-${width}.png`), fullPage: true })
  })
}

test('publishing, selecting history, export and restore keep revisions immutable', async ({ page, context }) => {
  const state = await mockLibrary(context)
  await page.goto('/settings/apps')
  await page.getByRole('button', { name: /Report.*Revision 1/ }).click()
  await page.getByLabel('Self-contained HTML', { exact: true }).fill('<h1>Revised report</h1>')
  await page.getByRole('button', { name: 'Publish new immutable revision', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Published revision 2.' })).toBeVisible()
  await page.getByLabel('Revision history', { exact: true }).selectOption('1')
  await expect(page.getByLabel('Self-contained HTML', { exact: true })).toHaveValue(initial.html)
  await expect(page.getByLabel('Self-contained HTML', { exact: true })).toHaveAttribute('readonly', '')
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export JSON', exact: true }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('report.app.json')
  const stream = await download.createReadStream(), chunks = []
  for await (const chunk of stream) chunks.push(chunk)
  expect(JSON.parse(Buffer.concat(chunks).toString())).toMatchObject({ version: 1, id: 'report', html: initial.html })
  await page.getByRole('button', { name: 'Restore as new revision', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Published revision 3.' })).toBeVisible()
  expect(state.writes.at(-1)).toMatchObject({ id: 'report', html: initial.html, expected_revision: 2, change_note: 'Restored revision 1' })
  expect(state.records.get('report').revisions.map(item => item.html)).toEqual([initial.html, '<h1>Revised report</h1>', initial.html])
})

test('failed publication retains draft; history refresh is explicit and archive is reversible', async ({ page, context }) => {
  const state = await mockLibrary(context)
  await page.goto('/settings/apps')
  await page.getByRole('button', { name: /Report.*Revision 1/ }).click()
  await page.getByLabel('Title', { exact: true }).fill('Unsaved title')
  state.fail = 'Revision conflict; refresh history before publishing.'
  await page.getByRole('button', { name: 'Publish new immutable revision', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Revision conflict')
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Unsaved title')
  state.fail = ''
  await page.getByRole('button', { name: 'Refresh history', exact: true }).click()
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Unsaved title')
  await page.getByRole('button', { name: 'Archive app', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Publish new immutable revision', exact: true })).toBeDisabled()
  await page.getByLabel('Show archived', { exact: true }).check()
  await expect(page.getByRole('button', { name: /Report.*Archived/ })).toBeVisible()
  await page.getByRole('button', { name: 'Unarchive app', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Publish new immutable revision', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'Discard draft', exact: true }).click()
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Report')
  expect(state.writes.at(-1)).toEqual({ id: 'report', archived: false, expected_revision: 1 })
})

test('import only fills an unsaved draft and malformed JSON blocks preview and publication', async ({ page, context }) => {
  const state = await mockLibrary(context, [])
  await page.goto('/settings/apps')
  await page.getByLabel('Import app package', { exact: true }).setInputFiles({ name: 'report.app.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ version: 1, ...initial })) })
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Report')
  await expect(page.getByRole('region', { name: 'Sample preview', exact: true })).toHaveCount(0)
  expect(state.writes).toHaveLength(0)
  await page.getByLabel('Sample data (JSON object)', { exact: true }).fill('{broken')
  await page.getByRole('button', { name: 'Preview sample', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Sample data must be valid JSON')
  await page.getByRole('button', { name: 'Publish new immutable revision', exact: true }).click()
  expect(state.writes).toHaveLength(0)
  await page.getByLabel('Import app package', { exact: true }).setInputFiles({ name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{}') })
  await expect(page.getByRole('alert')).toContainText('App packages need a slug')
  await expect(page.getByLabel('Sample data (JSON object)', { exact: true })).toHaveValue('{broken')
})

test('drafts survive app switching and history inspection without becoming published content', async ({ page, context }) => {
  await mockLibrary(context, [initial, { ...initial, id: 'other', title: 'Other app' }])
  await page.goto('/settings/apps')
  await page.getByRole('button', { name: /Report.*Revision 1/ }).click()
  await page.getByLabel('Title', { exact: true }).fill('Draft report')
  await page.getByRole('button', { name: /Other app.*Revision 1/ }).click()
  await expect(page.getByRole('button', { name: /Report.*Unsaved draft/ })).toBeVisible()
  await page.getByRole('button', { name: /Report.*Unsaved draft/ }).click()
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Draft report')
})

test('post-publication read failure never retries a successful write blindly', async ({ page, context }) => {
  const state = await mockLibrary(context)
  await page.goto('/settings/apps')
  await page.getByRole('button', { name: /Report.*Revision 1/ }).click()
  await page.getByLabel('Title', { exact: true }).fill('Published title')
  state.reloadFail = true
  await page.getByRole('button', { name: 'Publish new immutable revision', exact: true }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Published, but could not reload source' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Publish new immutable revision', exact: true })).toBeDisabled()
  state.reloadFail = false
  await page.getByRole('button', { name: 'Reload published source', exact: true }).click()
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Published title')
  expect(state.writes).toHaveLength(1)
})

test('members can open their own Apps library and discover its editor through settings search', async ({ page, context }) => {
  await mockLibrary(context, [])
  await page.route('**/api/auth/me', route => route.fulfill({ json: { ...fixtureAccount, user: { ...fixtureAccount.user, role: 'member' } } }))
  await page.goto('/settings/apps')
  await expect(page.getByRole('heading', { name: 'Apps', exact: true })).toBeVisible()
  const search = page.getByRole('searchbox', { name: 'Search settings' })
  await search.fill('immutable')
  await page.getByRole('link', { name: /App source & revisions/ }).click()
  await expect(page.getByRole('region', { name: 'App source & revisions', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Create app', exact: true }).click()
  await expect(page.getByLabel('Self-contained HTML', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Publish new immutable revision', exact: true })).toBeEnabled()
})

test('history inspection preserves the active draft and cannot restore over it accidentally', async ({ page, context }) => {
  const state = await mockLibrary(context)
  state.records.get('report').revisions.push({ ...initial, revision: 2, title: 'Active report', html: '<h1>Active report</h1>' })
  await page.goto('/settings/apps')
  await page.getByRole('button', { name: /Active report.*Revision 2/ }).click()
  await page.getByLabel('Title', { exact: true }).fill('Unpublished draft')
  await page.getByLabel('Revision history', { exact: true }).selectOption('1')
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Report')
  await expect(page.getByRole('button', { name: 'Restore as new revision', exact: true })).toBeDisabled()
  await page.getByLabel('Revision history', { exact: true }).selectOption('2')
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Unpublished draft')
  expect(state.writes).toHaveLength(0)
})
