import { test, expect } from './account-fixture.js'
import { PREFERENCES_KEY } from '../src/preferences-schema.js'
import { mockDesign } from './design-fixture.js'

const collapseButton = page => page.getByRole('button', { name: 'Collapse sidebar', exact: true })
const seedCollapsed = context => context.addInitScript(key => {
  if (!localStorage.getItem(key)) localStorage.setItem(key, JSON.stringify({ sideCollapsed: true, sideWidth: 330 }))
}, PREFERENCES_KEY)
const chatsLink = page => page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link', { name: 'Chats', exact: true })
const savedPreferences = page => page.evaluate(key => JSON.parse(localStorage.getItem(key)), PREFERENCES_KEY)

test('desktop header keeps workspace search and New chat without a sidebar toggle', async ({ page, context }) => {
  await mockDesign(context)
  await page.goto('/chats/alpha')
  const sidebar = page.locator('#shell-sidebar')
  const nav = page.getByRole('navigation', { name: 'Main navigation' })
  await expect(nav.getByRole('button', { name: /^(Collapse|Expand) sidebar$/ })).toHaveCount(0)
  await expect(nav.getByRole('button', { name: 'New chat', exact: true })).toBeVisible()
  await expect(nav.getByRole('link', { name: 'Settings', exact: true })).toBeVisible()
  const barBox = await nav.boundingBox()
  const searchBox = await nav.getByRole('button', { name: 'Search workspace', exact: true }).boundingBox()
  expect(searchBox.x + searchBox.width / 2).toBeCloseTo(barBox.x + barBox.width / 2, 0)
  expect(barBox.y + barBox.height).toBeCloseTo((await sidebar.boundingBox()).y, 0)
  const agents = sidebar.getByRole('navigation', { name: 'Chats by agent' })
  await expect(agents).toBeVisible()
  await expect(page.getByRole('separator', { name: 'Resize sidebar' })).toBeVisible()
})

test('collapse preference survives reload and restores the saved sidebar width', async ({ page, context }) => {
  const state = await mockDesign(context)
  await page.goto('/chats/alpha')
  const grip = page.getByRole('separator', { name: 'Resize sidebar' })
  await grip.press('ArrowRight')
  await expect(grip).toHaveAttribute('aria-valuenow', '330')
  await page.goto('/settings/workspace')
  await expect(page.locator('#shell-sidebar')).toHaveCount(0)
  await page.getByRole('switch', { name: 'Collapse sidebar', exact: true }).check()
  await expect.poll(async () => (await savedPreferences(page)).sideCollapsed).toBe(true)
  await page.goto('/chats/alpha')
  await expect(chatsLink(page)).toHaveAttribute('href', '/chats/alpha')
  await expect(page.locator('#shell-sidebar')).toBeHidden()
  await page.reload()
  await expect(page.getByRole('button', { name: 'Expand sidebar', exact: true })).toHaveCount(0)
  await expect(page.locator('#shell-sidebar')).toBeHidden()
  await expect(grip).toBeHidden()
  const input = page.getByRole('textbox', { name: 'Message', exact: true })
  await input.fill('Keep this draft while I focus')
  await expect(chatsLink(page)).toHaveAttribute('href', '/chats/alpha')
  await chatsLink(page).click()
  await expect(input).toHaveValue('Keep this draft while I focus')
  await expect(page).toHaveURL(/\/chats\/alpha$/)
  await expect(page.locator('#shell-sidebar')).toHaveCSS('width', '330px')
  await expect.poll(async () => (await savedPreferences(page)).sideCollapsed).toBe(false)
  await page.reload()
  await expect(page.locator('#shell-sidebar')).toHaveCSS('width', '330px')
  expect(state.writes).toEqual([])
})

for (const [section, start, heading, destination] of [
  ['Workflows', '/chats/alpha', 'Workflows', /\/workflows$/],
  ['Workflows', '/workflows?view=runs', 'Workflows', /\/workflows\?view=runs$/],
  ['Denkzeug', '/workflows', 'Chats', /\/chats$/]
]) {
  test(`${section} reopens a collapsed list from ${start}`, async ({ page, context }) => {
    await mockDesign(context)
    await seedCollapsed(context)
    await page.goto(start)
    const sidebar = page.locator('#shell-sidebar')
    const nav = page.getByRole('navigation', { name: 'Main navigation' })
    await expect(sidebar).toBeHidden()
    await nav.getByRole('link', { name: section, exact: true }).click()
    await expect(page).toHaveURL(destination)
    await expect(sidebar.getByRole('heading', { name: heading, exact: true })).toBeVisible()
    if (start.includes('?view=runs')) await expect(page.getByRole('heading', { name: 'All runs', exact: true })).toBeVisible()
  })
}

for (const interfaceSize of [125, 150]) {
  test(`top-bar actions fit at 901px and ${interfaceSize}% interface size`, async ({ page, context }) => {
    await mockDesign(context)
    await context.addInitScript(({ key, interfaceSize }) => {
      localStorage.setItem(key, JSON.stringify({ sideWidth: 260, interfaceSize }))
    }, { key: PREFERENCES_KEY, interfaceSize })
    await page.setViewportSize({ width: 901, height: 800 })
    await page.goto('/chats/alpha')
    const nav = page.getByRole('navigation', { name: 'Main navigation' })
    const title = page.locator('.side__head').getByRole('heading')
    for (const section of ['Chats', 'Workflows']) {
      await nav.getByRole('link', { name: section, exact: true }).click()
      await expect(title).toHaveText(section)
      await expect(title).toHaveCSS('text-overflow', 'ellipsis')
      for (const action of await nav.getByRole('link').or(nav.getByRole('button')).all()) {
        await expect(action).toBeInViewport({ ratio: 1 })
      }
      expect(await nav.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
    }
  })
}

for (const width of [390, 900]) {
  test(`desktop collapse does not hide mobile navigation at ${width}px`, async ({ page, context }) => {
    await mockDesign(context)
    await seedCollapsed(context)
    await page.goto('/chats/alpha')
    await expect.poll(async () => (await savedPreferences(page)).sideCollapsed).toBe(true)
    await page.setViewportSize({ width, height: 800 })
    await expect(collapseButton(page)).toHaveCount(0)
    await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Back to chats', exact: true }).click()
    await expect(page.locator('#shell-sidebar')).toBeVisible()
    await expect(collapseButton(page)).toHaveCount(0)
    await expect(page.getByRole('separator', { name: 'Resize sidebar' })).toBeHidden()
    const nav = page.getByRole('navigation', { name: 'Main navigation' })
    await expect(nav).toBeVisible()
    await nav.getByRole('link', { name: 'Workflows', exact: true }).click()
    await nav.getByRole('link', { name: 'Chats', exact: true }).click()
    await expect(page.locator('#shell-sidebar')).toBeVisible()
    await expect.poll(async () => (await savedPreferences(page)).sideCollapsed).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.reload()
    await expect(page.locator('#shell-sidebar')).toBeVisible()
    await page.setViewportSize({ width: 1440, height: 1000 })
    await expect(chatsLink(page)).toBeVisible()
    await expect(page.locator('#shell-sidebar')).toBeHidden()
  })
}
