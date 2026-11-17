import { test, expect } from '@playwright/test'
import { rolldown } from 'rolldown'
import { mockAgents } from './agent-fixture.js'
import { PREFERENCES_KEY } from '../src/preferences-schema.js'

let appHtml
// Exercise a real third-party protocol client (the official App SDK), not a
// Denkzeug-specific iframe handshake. No external MCP service is needed in CI.
test.beforeAll(async () => {
  const build = await rolldown({ input: new URL('./fixtures/mcp-app-client.js', import.meta.url).pathname })
  const { output } = await build.generate({ format: 'iife', minify: true })
  await build.close()
  appHtml = `<html><head><script>window.firstSurface = { scheme: getComputedStyle(document.documentElement).colorScheme, background: getComputedStyle(document.documentElement).backgroundColor }</script></head><body><h2 id="result">Waiting</h2><input aria-label="Local filter"><button id="copy">Copy Markdown</button><button id="link">Open release page</button><p id="status"></p><script>${output[0].code.replace(/<\/script/gi, '<\\/script')}</script></body></html>`
})

async function harness (page, context = { theme: 'light', displayMode: 'inline' }, html = appHtml) {
  await page.goto('/tests/fixtures/mcp-host.html')
  await page.evaluate(async ({ html, context }) => {
    const { connectApp } = await import('/src/mcp-apps.js')
    const frame = document.querySelector('#view')
    window.connection = await connectApp(frame, {
      html, input: { version: 'v1.2.3' },
      result: { content: [{ type: 'text', text: 'Original Markdown' }], structuredContent: { notes: '# A useful release', version: 'v1.2.3' } }
    }, 'http://127.0.0.1:8092', {
      context,
      resize: height => { frame.style.height = `${height}px` },
      ready: () => { document.querySelector('#host-status').textContent = 'Ready' },
      error: text => { document.querySelector('#host-status').textContent = text },
      openLink: url => { document.querySelector('#host-link').href = url; document.querySelector('#host-link').textContent = 'Release' }
    })
  }, { html, context })
}

test('official App SDK receives data, sizes, links and handles unsupported capabilities', async ({ page }) => {
  await harness(page)
  const view = page.frameLocator('#view').frameLocator('iframe')
  await expect(page.locator('#host-status')).toHaveText('Ready')
  await expect(view.locator('#result')).toHaveText('# A useful release')
  await view.locator('#link').click()
  await expect(page.locator('#host-link')).toHaveAttribute('href', 'https://github.com/example/project/releases/new')
  await expect(view.locator('#status')).toContainText('tools blocked')
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await view.locator('#copy').click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe('# A useful release')
  await expect(page.locator('#view')).toHaveJSProperty('height', '')
  await page.evaluate(() => window.connection.close())
})

test('dark surfaces exist before app scripts and change theme without reloading', async ({ page }) => {
  await harness(page, { theme: 'dark', styles: { variables: {
    '--color-background-primary': '#171b26', '--color-text-primary': '#edf0f8'
  } } })
  const frame = page.locator('#view')
  const proxy = page.frameLocator('#view')
  const view = proxy.frameLocator('iframe')
  await expect(view.locator('#result')).toHaveText('# A useful release')
  await expect(frame).toHaveCSS('color-scheme', 'dark')
  await expect(frame).toHaveCSS('background-color', 'rgb(23, 27, 38)')
  await expect(proxy.locator('html')).toHaveCSS('background-color', 'rgb(23, 27, 38)')
  await expect(proxy.locator('iframe')).toHaveCSS('color-scheme', 'dark')
  await expect(proxy.locator('iframe')).toHaveCSS('background-color', 'rgb(23, 27, 38)')
  await expect(view.locator('html')).toHaveCSS('color-scheme', 'dark')
  await expect(view.locator('html')).toHaveCSS('background-color', 'rgb(23, 27, 38)')
  await expect(view.locator('html')).toHaveCSS('color', 'rgb(237, 240, 248)')
  const content = page.frames().find(item => item.url() === 'about:srcdoc')
  expect(await content.evaluate(() => window.firstSurface)).toEqual({ scheme: 'dark', background: 'rgb(23, 27, 38)' })
  await view.getByRole('textbox', { name: 'Local filter' }).fill('preserved across themes')
  const original = await frame.getAttribute('src')
  for (const [theme, background, color, rgb] of [
    ['dark', '#14121d', '#efebf7', 'rgb(20, 18, 29)'],
    ['light', '#fafafa', '#111111', 'rgb(250, 250, 250)'],
    ['dark', '#101517', '#e5edee', 'rgb(16, 21, 23)']
  ]) {
    await page.evaluate(({ theme, background, color }) => window.connection.setContext({ theme, styles: { variables: {
      '--color-background-primary': background, '--color-text-primary': color
    } } }), { theme, background, color })
    await expect(frame).toHaveCSS('color-scheme', theme)
    await expect(frame).toHaveCSS('background-color', rgb)
    await expect(proxy.locator('html')).toHaveCSS('color-scheme', theme)
    await expect(proxy.locator('html')).toHaveCSS('background-color', rgb)
    await expect(proxy.locator('iframe')).toHaveCSS('background-color', rgb)
    await expect(view.locator('html')).toHaveCSS('color-scheme', theme)
    await expect(view.locator('html')).toHaveCSS('background-color', rgb)
    await expect(view.getByRole('textbox', { name: 'Local filter' })).toHaveValue('preserved across themes')
    await expect(frame).toHaveAttribute('src', original)
  }
  // The opaque app cannot forge a host context change back into the proxy.
  await content.evaluate(() => parent.postMessage({ jsonrpc: '2.0', method: 'ui/notifications/host-context-changed', params: { theme: 'light' } }, '*'))
  await expect(proxy.locator('html')).toHaveCSS('color-scheme', 'dark')
  await page.screenshot({ path: '/workspace/mcp-app-dark-surface.png' })
})

test('host surface defaults do not override an app explicit background', async ({ page }) => {
  await harness(page, { theme: 'dark' }, appHtml.replace('<head>', '<head><style>html { background-color: rgb(40, 50, 60); color: rgb(200, 210, 220); }</style>'))
  const view = page.frameLocator('#view').frameLocator('iframe')
  await expect(view.locator('#result')).toHaveText('# A useful release')
  await expect(view.locator('html')).toHaveCSS('background-color', 'rgb(40, 50, 60)')
  await expect(view.locator('html')).toHaveCSS('color', 'rgb(200, 210, 220)')
  await page.evaluate(() => window.connection.setContext({ theme: 'light' }))
  await expect(view.locator('html')).toHaveCSS('color-scheme', 'light')
  await expect(view.locator('html')).toHaveCSS('background-color', 'rgb(40, 50, 60)')
})

test('sandbox blocks parent access and unapproved network requests', async ({ page }) => {
  await harness(page)
  const view = page.frameLocator('#view').frameLocator('iframe')
  await expect(view.locator('#result')).toHaveText('# A useful release')
  const frame = page.frames().find(item => item.url() === 'about:srcdoc')
  const isolation = await frame.evaluate(async () => {
    let parentBlocked = false, networkBlocked = false
    try { parent.document.body.textContent } catch { parentBlocked = true }
    try { await fetch('https://example.com') } catch { networkBlocked = true }
    return { parentBlocked, networkBlocked, cookie: (() => { try { return document.cookie } catch { return 'blocked' } })() }
  })
  expect(isolation).toEqual({ parentBlocked: true, networkBlocked: true, cookie: 'blocked' })
  const guarded = await frame.evaluate(() => {
    const child = document.createElement('iframe')
    document.body.append(child)
    let nestedRTC = false
    try { nestedRTC = typeof child.contentWindow.RTCPeerConnection === 'function' } catch { /* opaque child */ }
    let replaceBlocked = false
    try { Object.defineProperty(globalThis, 'RTCPeerConnection', { value: 'restored' }) } catch { replaceBlocked = true }
    child.remove()
    return { rtc: typeof RTCPeerConnection, nestedRTC, replaceBlocked }
  })
  expect(guarded).toEqual({ rtc: 'undefined', nestedRTC: false, replaceBlocked: true })
  await page.screenshot({ path: '/workspace/mcp-app-host.png', fullPage: true })
})

test('saved workflow results and tool results share the inline host without replay', async ({ page, context }) => {
  // Three complete cold navigations: allow the host's 15s initialization
  // deadline per sandbox rather than coupling it to the 5s assertion default.
  test.setTimeout(60000)
  const state = await mockAgents(context)
  const descriptor = { id: 'saved-app', title: 'Release notes', app_id: 'release-notes', revision: 1 }
  state.data.messages = [{ id: 'result', role: 'assistant', content: 'Release prepared.', created_at: Date.now() / 1000, meta: { apps: [descriptor], origin: { kind: 'workflow', workflow: 'prepare-release', relation: 'result' } } }]
  let loads = 0
  const mutations = []
  page.on('request', request => { if (request.method() === 'POST' && /\/api\/(workflows|chats).*\/(run|messages)/.test(request.url())) mutations.push(request.url()) })
  await context.route('**/api/mcp-apps/runtime', route => route.fulfill({ json: { sandbox_url: 'http://127.0.0.1:8092' } }))
  await context.route('**/api/chats/alpha/apps/saved-app', route => {
    loads++
    return route.fulfill({ json: { html: appHtml, input: {}, title: 'Release notes', result: { content: [{ type: 'text', text: 'Release prepared.' }], structuredContent: { notes: '# A useful release' } } } })
  })
  await page.goto('/chats/alpha')
  const view = page.frameLocator('iframe[title="Release notes"]').frameLocator('iframe')
  await expect(view.locator('#result')).toHaveText('# A useful release', { timeout: 15000 })
  await page.reload()
  await expect(page.getByRole('region', { name: 'Release notes', exact: true })).toBeVisible({ timeout: 15000 })
  await expect(view.locator('#result')).toHaveText('# A useful release', { timeout: 15000 })
  expect(loads).toBe(2)
  expect(mutations).toEqual([])
  await page.screenshot({ path: '/workspace/mcp-app-chat.png', fullPage: true })
  // The tool-result adapter uses the identical component outside collapsed tool details.
  state.data.messages[0].meta = { steps: [{ kind: 'tool', id: 'call', name: 'weather_forecast', ok: true, result: 'Release prepared.', app: descriptor }] }
  await page.reload()
  await expect(page.getByRole('region', { name: 'Release notes', exact: true })).toBeVisible({ timeout: 15000 })
  await expect(view.locator('#result')).toHaveText('# A useful release', { timeout: 15000 })
  expect(mutations).toEqual([])
})

test('cards lazy-load once and retain dark frame surfaces and local state while scrolling', async ({ page, context }) => {
  await page.addInitScript(key => localStorage.setItem(key, JSON.stringify({ theme: 'denkzeug-night' })), PREFERENCES_KEY)
  const state = await mockAgents(context)
  const descriptor = { id: 'persistent-card', title: 'Persistent card', app_id: 'report', revision: 1 }
  state.data.messages = [
    { id: 'card-message', role: 'assistant', content: 'Saved result', created_at: Date.now() / 1000, meta: { apps: [descriptor] } },
    { id: 'later-message', role: 'assistant', content: Array.from({ length: 90 }, () => 'Later conversation content.').join('\n\n') + '\n\nEnd of scroll test.', created_at: Date.now() / 1000, meta: {} }
  ]
  let loads = 0
  const mutations = []
  page.on('request', request => { if (request.method() === 'POST' && /\/api\/(workflows|chats).*\/(run|messages)/.test(request.url())) mutations.push(request.url()) })
  await context.route('**/api/mcp-apps/runtime', route => route.fulfill({ json: { sandbox_url: 'http://127.0.0.1:8092' } }))
  await context.route('**/api/chats/alpha/apps/persistent-card', route => {
    loads++
    return route.fulfill({ json: { html: appHtml, input: {}, title: descriptor.title, result: { content: [], structuredContent: { notes: '# Persistent result' } } } })
  })
  await page.goto('/chats/alpha')
  const panel = page.locator('#chat-conversation-panel')
  const card = page.getByRole('region', { name: descriptor.title, exact: true })
  const end = page.getByText('End of scroll test.', { exact: true })
  await expect(end).toBeInViewport()
  await expect(card).not.toBeInViewport()
  expect(loads).toBe(0)
  await expect(card.getByText('Interactive result loads when visible.')).toHaveCount(1)

  await card.scrollIntoViewIfNeeded()
  const frame = page.locator('iframe[title="Persistent card"]')
  const view = page.frameLocator('iframe[title="Persistent card"]').frameLocator('iframe')
  await expect(view.locator('#result')).toHaveText('# Persistent result')
  await view.getByRole('textbox', { name: 'Local filter' }).fill('keep this filter')
  const originalFrame = await frame.elementHandle()
  expect(loads).toBe(1)

  const proxy = page.frameLocator('iframe[title="Persistent card"]')
  const expectDarkSurfaces = async () => {
    for (const layer of [frame, proxy.locator('html'), proxy.locator('iframe'), view.locator('html')]) {
      await expect(layer).toHaveCSS('color-scheme', 'dark')
      await expect(layer).toHaveCSS('background-color', 'rgb(23, 27, 38)')
    }
  }
  await expectDarkSurfaces()
  for (let cycle = 0; cycle < 4; cycle++) {
    await panel.evaluate(node => node.scrollTo({ top: node.scrollHeight, behavior: 'instant' }))
    await expect(end).toBeInViewport()
    await expect(card).not.toBeInViewport()
    // Let visibility observers run after the scroll without a timing sleep.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    await expect(frame).toBeAttached()
    expect(await originalFrame.evaluate(node => node.isConnected)).toBe(true)
    await expect(view.getByRole('textbox', { name: 'Local filter' })).toHaveValue('keep this filter')
    await card.scrollIntoViewIfNeeded()
    await expect(view.locator('#result')).toHaveText('# Persistent result')
    await expect(view.getByRole('textbox', { name: 'Local filter' })).toHaveValue('keep this filter')
    await expectDarkSurfaces()
    expect(loads).toBe(1)
  }
  // Exercise the real preferences watcher via the cross-tab storage path.
  const changeTheme = theme => page.evaluate(({ key, theme }) => {
    localStorage.setItem(key, JSON.stringify({ ...JSON.parse(localStorage.getItem(key)), theme }))
    window.dispatchEvent(new StorageEvent('storage', { key, storageArea: localStorage }))
  }, { key: PREFERENCES_KEY, theme })
  await changeTheme('denkzeug')
  await expect(page.locator('html')).toHaveCSS('color-scheme', 'light')
  await expect(view.locator('html')).toHaveCSS('color-scheme', 'light')
  await changeTheme('denkzeug-night')
  await expectDarkSurfaces()
  await expect(view.getByRole('textbox', { name: 'Local filter' })).toHaveValue('keep this filter')
  expect(loads).toBe(1)
  await page.screenshot({ path: '/workspace/mcp-app-dark-scroll.png' })
  // A normal live snapshot also replaces message/meta descriptor objects.
  state.data.messages[0].content = 'Saved result updated by live snapshot'
  await expect(page.getByText('Saved result updated by live snapshot', { exact: true })).toBeVisible()
  await expect(view.getByRole('textbox', { name: 'Local filter' })).toHaveValue('keep this filter')
  expect(loads).toBe(1)
  await page.getByRole('tab', { name: 'Outputs', exact: true }).click()
  await expect(frame).toBeAttached()
  await page.getByRole('tab', { name: 'Chat', exact: true }).click()
  await expect(view.getByRole('textbox', { name: 'Local filter' })).toHaveValue('keep this filter')
  expect(loads).toBe(1)
  expect(mutations).toEqual([])
  await page.getByRole('link', { name: 'Settings', exact: true }).click()
  await expect(frame).toHaveCount(0)
  expect(await originalFrame.evaluate(node => node.isConnected)).toBe(false)
})

test('same-origin and malformed sandbox requests fail closed', async ({ request }) => {
  const denied = await request.get('http://127.0.0.1:8092/sandbox.html?parent=https%3A%2F%2Fevil.example')
  expect(denied.status()).toBe(400)
  const injected = await request.get(`http://127.0.0.1:8092/sandbox.html?parent=http%3A%2F%2F127.0.0.1%3A9012&csp=${encodeURIComponent(JSON.stringify({ resourceDomains: ["https://example.com; script-src *"] }))}`)
  expect(injected.status()).toBe(400)
  const normal = await request.get('http://127.0.0.1:8092/sandbox.html?parent=http%3A%2F%2F127.0.0.1%3A9012')
  expect(normal.headers()['content-security-policy']).toContain("connect-src 'none'")
  expect(normal.headers()['content-security-policy']).toContain("base-uri 'none'")
})
