import { test, expect } from '@playwright/test'
import { rolldown } from 'rolldown'
import { mockAgents } from './agent-fixture.js'

let appHtml
// Exercise a real third-party protocol client (the official App SDK), not a
// Denkzeug-specific iframe handshake. No external MCP service is needed in CI.
test.beforeAll(async () => {
  const build = await rolldown({ input: new URL('./fixtures/mcp-app-client.js', import.meta.url).pathname })
  const { output } = await build.generate({ format: 'iife', minify: true })
  await build.close()
  appHtml = `<html><body><h2 id="result">Waiting</h2><button id="copy">Copy Markdown</button><button id="link">Open release page</button><p id="status"></p><script>${output[0].code.replace(/<\/script/gi, '<\\/script')}</script></body></html>`
})

async function harness (page) {
  await page.goto('/tests/fixtures/mcp-host.html')
  await page.evaluate(async html => {
    const { connectApp } = await import('/src/mcp-apps.js')
    const frame = document.querySelector('#view')
    window.connection = await connectApp(frame, {
      html, input: { version: 'v1.2.3' },
      result: { content: [{ type: 'text', text: 'Original Markdown' }], structuredContent: { notes: '# A useful release', version: 'v1.2.3' } }
    }, 'http://127.0.0.1:8092', {
      context: { theme: 'light', displayMode: 'inline' },
      resize: height => { frame.style.height = `${height}px` },
      ready: () => { document.querySelector('#host-status').textContent = 'Ready' },
      error: text => { document.querySelector('#host-status').textContent = text },
      openLink: url => { document.querySelector('#host-link').href = url; document.querySelector('#host-link').textContent = 'Release' }
    })
  }, appHtml)
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
  await expect(view.locator('#result')).toHaveText('# A useful release')
  await page.reload()
  await expect(view.locator('#result')).toHaveText('# A useful release')
  expect(loads).toBe(2)
  expect(mutations).toEqual([])
  await page.screenshot({ path: '/workspace/mcp-app-chat.png', fullPage: true })
  // The tool-result adapter uses the identical component outside collapsed tool details.
  state.data.messages[0].meta = { steps: [{ kind: 'tool', id: 'call', name: 'weather_forecast', ok: true, result: 'Release prepared.', app: descriptor }] }
  await page.reload()
  await expect(view.locator('#result')).toHaveText('# A useful release')
  expect(mutations).toEqual([])
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
