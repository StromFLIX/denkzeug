import { test, expect } from './account-fixture.js'

async function mockApi (context, health) {
  await context.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/system') return route.fulfill({ json: {
      components: health === 'unknown' ? [] : [{ name: 'temporal', status: health }]
    } })
    if (path === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: ': connected\n\n' })
    const key = path.split('/').pop()
    return route.fulfill({ json: key === 'catalog' ? { models: [], tools: [], agent_sets: [] } : { [key]: [] } })
  })
}

for (const width of [1440, 901, 900, 390]) {
  for (const health of ['ok', 'degraded', 'unknown']) {
    test(`settings navigation at ${width}px with ${health} health`, async ({ page, context }) => {
      await mockApi(context, health)
      await page.setViewportSize({ width, height: 900 })
      await page.goto('/chats')

      const desktop = width > 900
      const settings = page.getByRole('link', { name: 'Settings', exact: true })
      await expect(settings).toHaveCount(1)
      await expect(settings).toBeVisible()
      await expect(page.getByRole('button', { name: 'Account menu' })).toHaveCount(0)
      const rail = page.getByRole('navigation', { name: 'Main navigation' })
      await expect(rail.getByRole('link', { name: 'Settings', exact: true })).toBeVisible()
      await expect(settings).toHaveCSS('text-decoration-line', 'none')
      await settings.hover()
      await expect(settings).toHaveCSS('text-decoration-line', 'none')
      await settings.focus()
      await expect(settings).toHaveCSS('text-decoration-line', 'none')
      await expect(settings).toHaveAttribute('href', `/settings/${health === 'degraded' ? 'system' : 'account'}`)
      await expect(settings.locator('.dot')).toHaveCount(health === 'ok' ? 0 : 1)
      if (health === 'degraded') await expect(settings.locator('.dot')).toHaveClass(/dot--bad/)

      if (!desktop) {
        await expect(rail.getByRole('link').first()).toHaveAccessibleName('Settings')
        const dock = await rail.boundingBox()
        expect(dock.y).toBeGreaterThan(800)
        expect(dock.width).toBeLessThan(width)
      }

      await settings.press('Enter')
      await expect(page).toHaveURL(`/settings/${health === 'degraded' ? 'system' : 'account'}`)
      if (health !== 'degraded') {
        await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeInViewport()
        await expect(page.getByRole('button', { name: 'Switch workspace' })).toHaveCount(0)
      }
    })
  }
}

test('native workspace switching is available from the Settings session section', async ({ context, page }) => {
  await mockApi(context, 'ok')
  await context.addInitScript(() => {
    globalThis.denkzeugDesktop = {}
    localStorage.setItem('nautionette.server', location.origin)
  })
  await page.goto('/settings/account')
  const logout = page.waitForRequest(request => new URL(request.url()).pathname === '/api/auth/logout')
  await page.getByRole('button', { name: 'Switch workspace' }).click()
  expect((await logout).postDataJSON()).toEqual({ all_devices: false })
  await expect(page.getByRole('textbox', { name: 'Server address' })).toBeVisible()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('nautionette.server'))).toBeNull()
})

test('settings stays in the navigation when it moves between the top bar and floating dock', async ({ page, context }) => {
  await mockApi(context, 'ok')
  await page.goto('/chats')
  for (const width of [1440, 390, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await expect(page.getByRole('link', { name: 'Settings', exact: true })).toHaveCount(1)
    const links = page.getByRole('navigation', { name: 'Main navigation' }).getByRole('link')
    await expect(width > 900 ? links.last() : links.first()).toHaveAccessibleName('Settings')
  }
})
