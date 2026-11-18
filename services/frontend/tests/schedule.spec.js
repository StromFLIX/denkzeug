import { expect, test } from './account-fixture.js'

const legacy = {
  id: 'schedule-morning_digest', frequency: 'daily', at: '07:30', timezone: 'Europe/Berlin',
  input: { topic: 'engineering' }, description: 'Every day at 07:30', paused: false,
  next_run: '2026-09-08T05:30:00+00:00'
}

async function mockApi (page, initialSchedules = []) {
  const base = '/api/workflows/morning_digest'
  const state = { schedules: [...initialSchedules], request: null }
  let sequence = 0
  const workflow = () => ({
    name: 'morning_digest', title: 'Morning digest', description: 'Summarize updates.',
    code: 'MANIFEST = {}\n', graph: { nodes: [], edges: [] }, runs: [], settings: {},
    manifest: { inputs: { type: 'object', properties: { topic: { type: 'string' } } } },
    schedules: state.schedules, schedule_status: 'available'
  })

  await page.route('**/api/**', async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname
    if (path.startsWith('/api/auth/')) return route.fallback()
    if (path === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: ': connected\n\n' })
    let data = {
      '/api/system': { components: [], agent_sets: [] },
      '/api/catalog': { models: [], tools: [], agent_sets: [] },
      '/api/chats': { chats: [] }, '/api/drafts': { drafts: [] }, '/api/runs': { runs: [] },
      '/api/workflows': { workflows: [workflow()] }, [base]: workflow()
    }[path] || {}
    if (path === `${base}/schedules` && request.method() === 'POST') {
      state.request = request.postDataJSON()
      data = { ...state.request, id: `schedule-morning_digest--${++sequence}`, description: 'Tue, Thu at 07:15', paused: false, next_run: '2026-09-08T05:15:00+00:00' }
      state.schedules.push(data)
    }
    if (path.startsWith(`${base}/schedules/`)) {
      const id = path.split('/').pop()
      if (request.method() === 'PUT') {
        state.request = request.postDataJSON()
        const existing = state.schedules.find(entry => entry.id === id)
        data = { ...existing, ...state.request, description: `Every day at ${state.request.at}` }
        state.schedules = state.schedules.map(entry => entry.id === id ? data : entry)
      }
      if (request.method() === 'DELETE') {
        state.schedules = state.schedules.filter(entry => entry.id !== id)
        data = { ok: true }
      }
    }
    return route.fulfill({ json: data })
  })
  return state
}

const row = (page, description) => page.getByRole('listitem').filter({ hasText: description })
const settingsButton = page => page.getByRole('button', { name: 'Settings', exact: true })

test('Settings lists schedules; editing and removing one leaves the other intact', async ({ page }) => {
  const other = { ...legacy, id: 'schedule-morning_digest--other', at: '08:30', description: 'Every day at 08:30', input: { topic: 'support' }, timezone: 'Europe/Zurich', next_run: '2026-09-08T06:30:00+00:00' }
  const state = await mockApi(page, [other, legacy])
  await page.goto('/workflows/morning_digest')
  await expect(page.getByRole('heading', { name: 'Next execution' }).locator('..')).toContainText('Europe/Berlin')
  await settingsButton(page).click()
  await expect(page.getByRole('list', { name: 'Saved schedules' }).getByRole('listitem')).toHaveCount(2)
  await expect(row(page, legacy.description)).toContainText('Next run')
  await expect(page.getByLabel('Repeat')).toHaveCount(0)
  await page.screenshot({ path: '/tmp/nautionette-schedule-desktop.png' })

  await row(page, legacy.description).getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Edit schedule' })).toBeVisible()
  await expect(page.getByLabel('topic', { exact: true })).toHaveValue('engineering')
  await page.getByLabel('topic', { exact: true }).fill('discard this')
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await row(page, legacy.description).getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('topic', { exact: true })).toHaveValue('engineering')
  await page.getByLabel('At', { exact: true }).fill('09:30')
  await page.getByRole('button', { name: 'Update schedule' }).click()
  await expect(row(page, 'Every day at 09:30')).toBeVisible()
  await expect(row(page, other.description)).toBeVisible()
  expect(state.schedules.find(entry => entry.id === other.id)).toEqual(other)

  await row(page, 'Every day at 09:30').getByRole('button', { name: 'Remove', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Remove', exact: true }).click()
  await expect(row(page, 'Every day at 09:30')).toHaveCount(0)
  await page.reload()
  await expect(row(page, other.description)).toBeVisible()
  expect(state.schedules).toEqual([other])
})

test('opens a scrollable add form and creates another timezone-aware schedule on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 700 })
  const state = await mockApi(page, [legacy])
  await page.goto('/workflows/morning_digest')
  await settingsButton(page).click()
  await page.getByRole('button', { name: 'Add schedule', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Add schedule' })).toBeVisible()
  await expect(page.getByLabel('At', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('topic', { exact: true })).toHaveValue('')
  await expect(page.getByRole('button', { name: 'Save schedule' })).toBeDisabled()
  await page.getByLabel('Repeat').selectOption('weekly')
  await page.getByLabel('At', { exact: true }).fill('07:15')
  await page.getByLabel('Timezone').fill('Europe/Berlin')
  await page.getByRole('button', { name: 'Tue', exact: true }).click()
  await page.getByRole('button', { name: 'Thu', exact: true }).click()
  await page.screenshot({ path: '/tmp/nautionette-schedule-mobile.png' })
  await page.getByRole('button', { name: 'Save schedule' }).click()

  await expect.poll(() => state.request).toMatchObject({
    frequency: 'weekly', days: ['tuesday', 'thursday'], timezone: 'Europe/Berlin', input: {}
  })
  expect(state.request.at).toMatch(/^07:15(:00)?$/)
  await expect(settingsButton(page)).toHaveClass(/tab--active/)
  await expect(row(page, 'Tue, Thu at 07:15')).toBeVisible()
  await expect(row(page, legacy.description)).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('requires an explicit replace action for an advanced schedule', async ({ page }) => {
  await mockApi(page, [{ ...legacy, frequency: 'custom', description: 'Custom schedule', at: undefined }])
  await page.goto('/workflows/morning_digest?tab=Settings')
  await expect(page.getByLabel('Repeat')).toHaveCount(0)
  await page.getByRole('button', { name: 'Replace schedule' }).click()
  await expect(page.getByText('Saving replaces this custom recurrence.', { exact: false })).toBeVisible()
  await expect(page.getByLabel('Repeat')).toHaveValue('daily')
  await expect(page.getByLabel('At', { exact: true })).toHaveValue('')
  await expect(page.getByRole('button', { name: 'Update schedule' })).toBeDisabled()
})
