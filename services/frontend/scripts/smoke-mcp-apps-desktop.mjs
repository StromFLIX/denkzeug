import { _electron as electron, expect } from '@playwright/test'
import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { nativeAppFixtures } from './mcp-app-fixtures.js'

// Exercise the real bundled origin, isolated preload and Electron policy—not
// a web page with a fake native flag. The sandbox is the actual stdlib service.
const fixtures = await nativeAppFixtures()
const profile = await mkdtemp(resolve(tmpdir(), 'denkzeug-mcp-desktop-'))
const sandbox = spawn('python3', [resolve('../backend/nautionette_backend/app_sandbox.py')], {
  env: { PATH: process.env.PATH, PORT: '8092', MCP_APPS_HOST_ORIGINS: 'https://web-host.example', MCP_APPS_INCLUDE_NATIVE_ORIGINS: 'true' },
  stdio: ['ignore', 'pipe', 'pipe']
})
let application
try {
  await expect.poll(async () => {
    if (sandbox.exitCode !== null) throw new Error('Desktop test sandbox exited before readiness (port 8092 must be free).')
    try { return (await fetch('http://127.0.0.1:8092/healthz')).status } catch { return 0 }
  }).toBe(200)
  const rejected = await fetch('http://127.0.0.1:8092/sandbox.html?' + new URLSearchParams({ parent: 'null', csp: '{}' }))
  expect(rejected.status).toBe(400)
  application = await electron.launch({
    executablePath: process.env.ELECTRON_EXECUTABLE || resolve('out/desktop/linux-unpacked/nautionette-frontend'),
    // CI-only OS sandbox accommodation, identical to the existing smoke. Never
    // alter packaged BrowserWindow webSecurity/sandbox/Node/IPC preferences.
    args: [...(process.getuid?.() === 0 || process.env.ELECTRON_TEST_NO_SANDBOX === '1' ? ['--no-sandbox'] : []), ...(process.env.ELECTRON_EXECUTABLE ? ['.'] : [])],
    env: { ...process.env, XDG_CONFIG_HOME: profile }, timeout: 30000
  })
  const page = await application.firstWindow()
  await application.evaluate(({ BrowserWindow, shell }) => {
    const window = BrowserWindow.getAllWindows()[0]
    window.webContents.setBackgroundThrottling(false)
    window.show(); window.focus()
    globalThis.mcpExternalLinks = []
    shell.openExternal = async url => { globalThis.mcpExternalLinks.push(url) }
  })
  await expect(page.getByPlaceholder('https://denkzeug.example.com')).toBeVisible({ timeout: 20000 })
  expect(await application.evaluate(({ BrowserWindow }) => {
    const prefs = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences()
    return { node: prefs.nodeIntegration, subframes: prefs.nodeIntegrationInSubFrames, isolation: prefs.contextIsolation, sandbox: prefs.sandbox, webSecurity: prefs.webSecurity }
  })).toEqual({ node: false, subframes: false, isolation: true, sandbox: true, webSecurity: true })
  expect(await page.evaluate(() => ({ origin: location.origin, urlOrigin: new URL(location.href).origin, node: typeof require, oauth: globalThis.denkzeugDesktop?.oauth?.version })))
    .toEqual({ origin: 'capacitor://localhost', urlOrigin: 'capacitor://localhost', node: 'undefined', oauth: 1 })
  await page.addScriptTag({ content: fixtures.host })
  await page.evaluate(async html => {
    const frame = document.createElement('iframe')
    frame.id = 'mcp-native-smoke'; frame.title = 'Native card'
    frame.setAttribute('sandbox', 'allow-scripts allow-same-origin')
    frame.style.cssText = 'position:fixed;inset:50px 20px;z-index:99999;width:800px;height:400px;border:0;background:white'
    document.body.append(frame)
    window.mcpNativeConnection = await window.DenkzeugMcpHostTest.connectApp(frame, {
      html, input: { client: 'desktop' }, result: { content: [], structuredContent: { notes: '# Native desktop result' } }
    }, 'http://127.0.0.1:8092', {
      context: { platform: 'desktop', theme: 'dark', displayMode: 'inline', styles: { variables: {
        '--color-background-primary': '#171b26', '--color-text-primary': '#edf0f8'
      } } },
      resize: height => { frame.style.height = `${height}px` },
      ready: () => { window.mcpNativeReady = true }, error: message => { window.mcpNativeError = message },
      openLink: url => { window.open(url, '_blank', 'noopener,noreferrer') }
    })
  }, fixtures.html)
  await expect.poll(() => page.evaluate(() => window.mcpNativeError || window.mcpNativeReady)).toBe(true)
  const outer = page.locator('#mcp-native-smoke')
  expect(new URL(await outer.getAttribute('src')).searchParams.get('parent')).toBe('capacitor://localhost')
  const proxy = page.frameLocator('#mcp-native-smoke')
  await expect(proxy.locator('iframe')).toHaveAttribute('sandbox', 'allow-scripts')
  const view = proxy.frameLocator('iframe')
  await expect(view.locator('#result')).toHaveText('# Native desktop result')
  await expect(view.locator('#status')).toHaveText('tools blocked')
  await view.locator('#copy').click()
  expect(await application.evaluate(({ clipboard }) => clipboard.readText())).toBe('# Native desktop result')
  const child = page.frames().find(frame => frame.url() === 'about:srcdoc')
  expect(await child.evaluate(() => window.firstSurface)).toEqual({ scheme: 'dark', background: 'rgb(23, 27, 38)' })
  const originalSrc = await outer.getAttribute('src')
  for (const [theme, background, color, rgb] of [
    ['dark', '#171b26', '#edf0f8', 'rgb(23, 27, 38)'],
    ['light', '#fafafa', '#111111', 'rgb(250, 250, 250)'],
    ['dark', '#171b26', '#edf0f8', 'rgb(23, 27, 38)']
  ]) {
    await page.evaluate(({ theme, background, color }) => window.mcpNativeConnection.setContext({ theme, styles: { variables: {
      '--color-background-primary': background, '--color-text-primary': color
    } } }), { theme, background, color })
    for (const layer of [outer, proxy.locator('html'), proxy.locator('iframe'), view.locator('html')]) {
      await expect(layer).toHaveCSS('color-scheme', theme)
      await expect(layer).toHaveCSS('background-color', rgb)
    }
    await expect(outer).toHaveAttribute('src', originalSrc)
    await expect(view.locator('#result')).toHaveText('# Native desktop result')
  }
  expect(await child.evaluate(() => {
    let hostBlocked = false
    try { parent.parent.document.body } catch { hostBlocked = true }
    return { node: typeof require, process: typeof process, desktop: typeof denkzeugDesktop, legacy: typeof nautionetteDesktop, hostBlocked }
  })).toEqual({ node: 'undefined', process: 'undefined', desktop: 'undefined', legacy: 'undefined', hostBlocked: true })
  // Direct inner-frame messages cannot impersonate the host-controlled proxy.
  await child.evaluate(() => parent.parent.postMessage({ jsonrpc: '2.0', id: 987, method: 'ui/open-link', params: { url: 'https://forged.example' } }, 'capacitor://localhost'))
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  expect(await application.evaluate(() => globalThis.mcpExternalLinks)).toEqual([])
  await view.locator('#link').click()
  await expect.poll(() => application.evaluate(() => globalThis.mcpExternalLinks)).toEqual(['https://github.com/example/project/releases/new'])
  await page.evaluate(() => window.mcpNativeConnection.close())
  console.log('Desktop MCP Apps: actual capacitor origin, dark first paint, live theme surfaces without reload, SDK handshake/data, strict RPC binding, external link and Node/preload isolation passed')

  // Model Android's local app-asset origin in a separate secure Chromium
  // session. No TLS exceptions or remote API credentials are used. This is
  // origin/protocol coverage, not a claim of physical Android device testing.
  const mobileWindow = application.waitForEvent('window')
  await application.evaluate(async ({ BrowserWindow, session }) => {
    const mobileSession = session.fromPartition('mcp-android-origin-test')
    await mobileSession.protocol.handle('https', request => {
      if (new URL(request.url).origin !== 'https://localhost') return new Response('Not found', { status: 404 })
      return new Response('<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="frame-src http://127.0.0.1:8092"></head><body>Android origin fixture</body></html>', { headers: { 'Content-Type': 'text/html' } })
    })
    const window = new BrowserWindow({ width: 420, height: 900, show: true, webPreferences: { session: mobileSession, nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true } })
    globalThis.mcpMobileWindowId = window.id
    window.webContents.setBackgroundThrottling(false)
    await window.loadURL('https://localhost/chats/native-test')
  })
  const mobile = await mobileWindow
  await mobile.addScriptTag({ content: fixtures.host })
  await mobile.evaluate(async html => {
    const frame = document.createElement('iframe')
    frame.id = 'mcp-android-smoke'; frame.setAttribute('sandbox', 'allow-scripts allow-same-origin'); document.body.append(frame)
    window.connection = await window.DenkzeugMcpHostTest.connectApp(frame, { html, result: { content: [], structuredContent: { notes: '# Android origin result' } } }, 'http://127.0.0.1:8092', {
      context: { platform: 'mobile', theme: 'light', displayMode: 'inline' }, resize () {}, openLink () {},
      ready: () => { window.cardReady = true }, error: message => { window.cardError = message }
    })
  }, fixtures.html)
  await expect.poll(() => mobile.evaluate(() => window.cardError || window.cardReady)).toBe(true)
  expect(new URL(await mobile.locator('#mcp-android-smoke').getAttribute('src')).searchParams.get('parent')).toBe('https://localhost')
  await expect(mobile.frameLocator('#mcp-android-smoke').frameLocator('iframe').locator('#result')).toHaveText('# Android origin result')
  await mobile.evaluate(() => window.connection.close())
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.fromId(globalThis.mcpMobileWindowId).close())
  console.log('Mobile MCP Apps: exact Android https://localhost origin completed the real sandbox/SDK handshake; physical devices not exercised')
} finally {
  await application?.close()
  sandbox.kill('SIGTERM')
  await rm(profile, { recursive: true, force: true })
}
