import { test, expect } from './account-fixture.js'
import { mockAgents } from './agent-fixture.js'
import { mockDesign } from './design-fixture.js'
import { PREFERENCES_KEY } from '../src/preferences-schema.js'
import { workspaceSearch } from './helpers.js'

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64')
test.use({ hasTouch: true, isMobile: true, viewport: { width: 320, height: 568 } })

test('native first launch tests the server before offering invitation and reset paths', async ({ context, page }) => {
  const server = 'https://workspace.example.test'
  await context.addInitScript(() => { window.Capacitor = { isNativePlatform: () => true, getPlatform: () => 'ios' } })
  const calls = []
  let available = false
  await page.route(`${server}/api/auth/**`, route => {
    calls.push(new URL(route.request().url()).pathname)
    return route.fulfill({ status: available ? 200 : 503, headers: { 'Access-Control-Allow-Origin': '*' }, json: available
      ? { setup_required: false, registration_mode: 'invite_only', mail_available: true, native_min_version: 1 }
      : { detail: 'Workspace is not reachable' } })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Connect to your workspace' })).toBeVisible()
  await expect(page.getByLabel('Username', { exact: true })).toHaveCount(0)
  const address = page.getByLabel('Server address')
  await address.fill(server)
  await page.getByRole('button', { name: 'Connect', exact: true }).tap()
  await expect(page.getByRole('alert')).toHaveText('Workspace is not reachable')
  expect(await page.evaluate(() => localStorage.getItem('nautionette.server'))).toBeNull()
  await expect(address).toHaveValue(server)
  available = true
  await page.getByRole('button', { name: 'Connect', exact: true }).tap()
  const invite = page.getByRole('button', { name: 'Use an invitation', exact: true })
  await expect(invite).toBeInViewport({ ratio: 1 })
  await invite.tap()
  await expect(page.getByLabel('Invitation code or link')).toBeVisible()
  await page.getByRole('button', { name: 'Back to sign in' }).tap()
  await page.getByRole('button', { name: 'Forgot password?' }).tap()
  await expect(page.getByLabel('Recovery email', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Use a reset code' })).toBeVisible()
  expect(calls).toEqual(['/api/auth/meta', '/api/auth/meta'])
  await page.getByRole('button', { name: 'Change server' }).tap()
  await expect(address).toBeVisible()
})

test('long drafts with four attachments keep Send and chat visible at keyboard height', async ({ page, context }, testInfo) => {
  const state = await mockDesign(context)
  await context.route('**/api/chats/alpha/draft', route => route.fulfill({ json: { revision: 0, text: '', attachments: [] } }))
  await page.goto('/chats/alpha')
  const editor = page.getByRole('textbox', { name: 'Message', exact: true })
  await editor.fill('Please summarize the decisions we made and link to the supporting notes. '.repeat(16))
  await page.locator('.composer input[type=file]').setInputFiles(Array.from({ length: 4 }, (_, i) => ({ name: `review-${i}.png`, mimeType: 'image/png', buffer: png })))
  await page.setViewportSize({ width: 320, height: 300 })
  const send = page.getByRole('button', { name: 'Send message', exact: true })
  await expect(send).toBeEnabled()
  await expect(send).toBeInViewport({ ratio: 1 })
  await expect(editor).toBeInViewport({ ratio: 1 })
  await expect(page.locator('.thread__body')).toBeInViewport({ ratio: 1 })
  expect((await page.locator('.thread__body').boundingBox()).height).toBeGreaterThanOrEqual(44)
  const strip = page.locator('.composer__attachments')
  await expect(strip).toHaveCSS('flex-wrap', 'nowrap')
  await expect(strip).toBeInViewport({ ratio: 1 })
  await expect(strip.locator('.composer__attachment')).toHaveCount(4)
  expect((await strip.boundingBox()).height).toBeLessThan(80)
  await page.screenshot({ path: testInfo.outputPath('keyboard-composer.png') })
  expect(state.sent).toEqual([])
})

test('workflow title, run action and settings tab fit on a narrow phone', async ({ page, context }, testInfo) => {
  const state = await mockDesign(context)
  state.workflow.schedule = { description: 'Every weekday at 09:00', timezone: 'Europe/Berlin', next_run: Date.now() / 1000 + 300 }
  await page.goto('/workflows/digest')
  const title = page.locator('.workflow-head__identity .pane-head__title')
  await expect(title).toHaveText('Morning digest')
  expect(await title.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
  await expect(page.getByRole('button', { name: 'Run workflow' })).toBeInViewport({ ratio: 1 })
  const settings = page.getByRole('navigation', { name: 'Workflow views' }).getByRole('button', { name: 'Settings', exact: true })
  await expect(settings).toBeInViewport({ ratio: 1 })
  await page.screenshot({ path: testInfo.outputPath('workflow-320.png') })
  await settings.tap()
  await expect(page.getByRole('heading', { name: 'Schedules', exact: true })).toBeVisible()
})

test('landscape chat filters do not shrink the list and apply without leaving it', async ({ page, context }, testInfo) => {
  await mockDesign(context)
  await page.setViewportSize({ width: 844, height: 390 })
  await page.goto('/chats')
  const list = page.locator('.side__list')
  await expect(list.getByText('Design review', { exact: true })).toBeVisible()
  const before = (await list.boundingBox()).height
  await page.getByRole('button', { name: 'Group', exact: true }).tap()
  const sheet = page.getByRole('dialog', { name: 'Chat list filters' })
  await expect(sheet.getByRole('button', { name: 'Show chats', exact: true })).toBeInViewport({ ratio: 1 })
  expect((await list.boundingBox()).height).toBeCloseTo(before)
  await page.screenshot({ path: testInfo.outputPath('landscape-filter-sheet.png') })
  await sheet.getByRole('button', { name: 'Activity', exact: true }).tap()
  await sheet.getByRole('button', { name: 'Show chats', exact: true }).tap()
  await expect(sheet).toBeHidden()
  await expect(page.getByText('Grouped by activity', { exact: true })).toBeVisible()
  await expect(list.getByText('Design review', { exact: true })).toBeInViewport()
  await page.screenshot({ path: testInfo.outputPath('landscape-filters.png') })
})

test('settings Back restores agent query and sign-out actions fit at 150%', async ({ page, context }) => {
  const state = await mockAgents(context)
  const agent = state.addAgent('Research Assistant')
  await context.addInitScript(key => localStorage.setItem(key, JSON.stringify({ interfaceSize: 150 })), PREFERENCES_KEY)
  await page.goto(`/chats?agent=${agent.id}`)
  await expect(page.getByRole('heading', { name: agent.name, exact: true })).toBeVisible()
  await page.getByRole('link', { name: 'Settings', exact: true }).tap()
  await page.getByRole('button', { name: 'Search settings', exact: true }).tap()
  const search = workspaceSearch(page)
  await search.getByRole('combobox').fill('devices sessions')
  await search.getByRole('option', { name: 'Devices & sessions', exact: true }).tap()
  await expect(page).toHaveURL('/settings/account#account-sessions')
  const signOut = page.getByRole('button', { name: 'Sign out all devices', exact: true })
  await signOut.scrollIntoViewIfNeeded()
  await expect(signOut).toBeInViewport({ ratio: 1 })
  expect(await page.locator('.settings__body').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
  await page.getByRole('button', { name: 'Back to workspace', exact: true }).tap()
  await expect(page).toHaveURL(`/chats?agent=${agent.id}`)
  await expect(page.getByRole('button', { name: `Chats with ${agent.name}` })).toHaveAttribute('aria-pressed', 'true')
})
