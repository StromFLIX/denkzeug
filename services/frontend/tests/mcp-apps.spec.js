import { test, expect } from '@playwright/test'
import { rolldown } from 'rolldown'

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
  await page.screenshot({ path: '/workspace/mcp-app-host.png', fullPage: true })
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
