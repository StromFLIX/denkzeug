import { test, expect } from './account-fixture.js'
import { createSSEFixture, snapshotFrame } from './sse-fixture.js'
import { readFile } from 'node:fs/promises'
import { PREFERENCES_KEY } from '../src/preferences-schema.js'

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==', 'base64')
const pdf = Buffer.concat([Buffer.from('%PDF-generated-output'), Buffer.from([0, 128, 255])])
function output (revision = 1, overrides = {}) {
  return {
    id: 'a'.repeat(32), revision, title: 'Mounting bracket', summary: 'Drawing and editable source.', source_app: 'CADCraft', turn_id: 'turn', created_at: Date.now() / 1000,
    files: [
      { id: `pdf-${revision}`, name: 'bracket.pdf', size: pdf.length, mime_type: 'application/pdf', role: 'file', previewable: false },
      { id: `preview-${revision}`, name: 'preview.png', size: png.length, mime_type: 'image/png', role: 'preview', previewable: true },
      { id: `source-${revision}`, name: 'bracket.dxf', size: 100, mime_type: 'image/vnd.dxf', role: 'source', previewable: false }
    ], ...overrides
  }
}
async function fixture (context, { empty = false, inline = false } = {}) {
  const sse = await createSSEFixture(context)
  const first = output()
  const chat = id => ({ id, title: id, agent_set: 'default', model: 'test/model', read_revision: 0, created_at: Date.now() / 1000, updated_at: Date.now() / 1000 })
  const state = {
    chats: {
      alpha: { chat: chat('alpha'), messages: [{ id: 'reply', role: 'assistant', content: 'Here is your result.', meta: inline ? { outputs: [first] } : {} }], outputs: empty ? [] : [first], active_turn: null },
      beta: { chat: chat('beta'), messages: [], outputs: [], active_turn: null }
    },
    history: [first], events: [], reads: [], files: [], fileBodies: new Map(), completedFiles: 0, failPreview: false, failDownload: false, failHistory: false
  }
  await context.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/events') return sse(route, () => state.events.splice(0).map(event => `data: ${JSON.stringify(event)}\n\n`).join(''), { deduplicate: false })
    if (path === '/api/catalog') return route.fulfill({ json: { models: [], tools: [], agent_sets: [], default_model: 'test/model' } })
    if (path === '/api/system') return route.fulfill({ json: { components: [], agent_sets: [] } })
    if (path === '/api/chats') return route.fulfill({ json: { chats: Object.values(state.chats).map(data => data.chat) } })
    if (path === '/api/workflows') return route.fulfill({ json: { workflows: [] } })
    if (path === '/api/drafts') return route.fulfill({ json: { drafts: [] } })
    if (path === '/api/runs') return route.fulfill({ json: { runs: [] } })
    if (path.includes('/outputs/')) {
      if (path.endsWith('/revisions')) return state.failHistory ? route.fulfill({ status: 503, json: { detail: 'History unavailable' } }) : route.fulfill({ json: { revisions: state.history } })
      const preview = new URL(route.request().url()).searchParams.get('preview') === 'true'
      state.files.push({ path, preview, authorization: route.request().headers().authorization })
      if (preview ? state.failPreview : state.failDownload) return route.fulfill({ status: 503, json: { detail: 'File unavailable' } })
      if (state.waitFile) await state.waitFile
      const body = preview || path.includes('/files/preview-') ? png : (state.fileBodies.get(path.split('/').pop()) || state.downloadBytes || pdf)
      await route.fulfill({ contentType: preview ? 'image/png' : 'application/octet-stream', body })
      state.completedFiles++
      return
    }
    const match = path.match(/^\/api\/chats\/([^/]+)(?:\/(stream|read-state))?$/)
    if (match) {
      const data = state.chats[match[1]]
      if (match[2] === 'stream') return sse(route, () => snapshotFrame(state.chats[match[1]]))
      if (match[2] === 'read-state') {
        state.reads.push(route.request().postDataJSON())
        return route.fulfill({ json: data.chat })
      }
      return route.fulfill({ json: data })
    }
    return route.fulfill({ json: {} })
  })
  await context.addInitScript(() => localStorage.setItem('nautionette.token', 'output-test-token'))
  return state
}

async function mockNativeDownloads (context, { available = true, legacy = false } = {}) {
  await context.addInitScript(({ available, legacy }) => {
    if (window !== window.top) return
    localStorage.setItem('nautionette.server', location.origin)
    window.androidBridge = {}
    window.nativeSaves = []
    window.nativeChunks = []
    window.nativeDiscards = []
    window.stagedBytes = ''
    window.saveOutcome = 'pending'
    window.Capacitor = {
      isNativePlatform: () => true,
      PluginHeaders: available ? [{ name: 'NautionetteDownloads', methods: (legacy ? ['save'] : ['begin', 'append', 'save', 'discard']).map(name => ({ name, rtype: 'promise' })) }] : [],
      nativePromise: async (plugin, method, options) => {
        if (plugin !== 'NautionetteDownloads') throw new Error('Unexpected native call')
        if (method === 'begin') {
          window.stagedBytes = ''
          return { id: 'staged-download' }
        }
        if (method === 'append') {
          window.nativeChunks.push(options)
          if (window.failChunk) throw new Error('Temporary storage is full')
          if (options.offset !== window.stagedBytes.length) throw new Error('Incorrect chunk offset')
          window.stagedBytes += atob(options.base64)
          if (window.pauseChunk) await new Promise(resolve => { window.finishChunk = resolve })
          return {}
        }
        if (method === 'discard') {
          window.nativeDiscards.push(options.id)
          return {}
        }
        if (method !== 'save') throw new Error('Unexpected native call')
        window.nativeSaves.push(options)
        if (window.saveOutcome === 'pending') return new Promise((resolve, reject) => { window.finishSave = resolve; window.failSave = reject })
        if (window.saveOutcome === 'failed') throw new Error('Storage is full')
        return { cancelled: window.saveOutcome === 'cancelled' }
      }
    }
  }, { available, legacy })
}

const outputsTab = page => page.getByRole('tab', { name: /Workspace/ })
const outputPanel = page => page.getByRole('tabpanel', { name: /Workspace/ })
const outputDetails = page => page.getByRole('dialog', { name: 'Output details', exact: true })
const fileDialog = page => page.getByRole('dialog').filter({ hasNot: page.getByRole('button', { name: 'All outputs', exact: true }) })
async function openBracket (page) {
  await outputsTab(page).click()
  await outputPanel(page).getByRole('button', { name: 'Open Mounting bracket', exact: true }).click()
  return outputDetails(page)
}

for (const width of [1440, 390]) {
  test(`outputs preview, download and composer state work at ${width}px without a browser app`, async ({ page, context }) => {
    const state = await fixture(context)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/chats/alpha')
    const composer = page.getByRole('textbox', { name: 'Message', exact: true })
    await composer.fill('Keep this draft')
    expect(state.files).toHaveLength(0)
    const panel = await openBracket(page)
    const title = panel.getByRole('heading', { name: 'Mounting bracket' })
    await expect(title).toBeInViewport()
    const drawer = panel.locator('.workspace-detail')
    await expect.poll(async () => {
      const box = await drawer.boundingBox()
      return box && [Math.round(box.x + box.width), Math.round(box.y), Math.round(box.height)]
    }).toEqual([width, 0, 900])
    if (width === 390) expect((await drawer.boundingBox()).width).toBe(width)
    await expect(panel.getByText('Ready · Revision 1 · CADCraft')).toBeVisible()
    await expect(panel.locator('img')).toBeVisible()
    await expect.poll(() => panel.locator('img').evaluate(image => image.naturalWidth)).toBe(1)
    await panel.getByRole('button', { name: 'View preview.png', exact: true }).click()
    await expect(fileDialog(page)).toBeVisible()
    await page.getByRole('button', { name: 'Close preview', exact: true }).click()
    const downloaded = page.waitForEvent('download')
    await panel.getByRole('button', { name: 'Download bracket.pdf', exact: true }).click()
    const download = await downloaded
    expect(download.suggestedFilename()).toBe('bracket.pdf')
    expect(await readFile(await download.path())).toEqual(pdf)
    await expect(panel.getByText('Editable source ·', { exact: false })).toBeVisible()
    expect(state.files.every(file => file.authorization === undefined)).toBe(true)
    expect(state.files.filter(file => !file.preview)).toHaveLength(1)
    await expect(page.locator('iframe, canvas')).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: `test-results/chat-outputs-${width}.png` })
    await panel.getByRole('button', { name: 'All outputs', exact: true }).click()
    await expect(panel).toHaveCount(0)
    await expect(outputPanel(page).getByRole('button', { name: 'Open Mounting bracket', exact: true })).toBeFocused()
    await page.getByRole('tab', { name: 'Chat', exact: true }).click()
    await expect(composer).toHaveValue('Keep this draft')
    expect(errors).toEqual([])
  })
}

for (const width of [1440, 390, 320]) {
  for (const mode of ['inline', 'thumbnails']) {
    test(`${mode} output rows stay compact and open exact revisions at ${width}px`, async ({ page, context }) => {
      const state = await fixture(context, { inline: true })
      state.chats.alpha.outputs = [output(2)]
      state.history.unshift(output(2))
      await page.setViewportSize({ width, height: 900 })
      await page.goto('/settings/workspace')
      const setting = page.getByRole('combobox', { name: 'Output display', exact: true })
      await expect(setting).toHaveValue('inline')
      await setting.selectOption(mode)
      await page.reload()
      await expect(setting).toHaveValue(mode)
      await page.goto('/chats/alpha')
      const row = page.getByRole('button', { name: 'View output: Mounting bracket', exact: true })
      await expect(row).toBeVisible()
      await expect(row).not.toContainText('Ready')
      await expect(row).not.toContainText('CADCraft')
      await expect(page.getByText('View files in Outputs')).toHaveCount(0)
      await expect(row.locator('button')).toHaveCount(0)
      if (mode === 'thumbnails') {
        await expect(row.locator('img')).toBeVisible()
        const image = await row.locator('img').boundingBox()
        expect(image.height).toBeLessThanOrEqual(44)
        expect(image.width).toBeLessThanOrEqual(54)
        expect(state.files).toHaveLength(1)
        expect(state.files[0].authorization).toBeUndefined()
        expect(state.files[0].preview).toBe(true)
      } else {
        await expect(row.locator('img')).toHaveCount(0)
        expect(state.files).toHaveLength(0)
      }
      expect((await row.boundingBox()).height).toBeLessThan(90)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: `test-results/output-rows-${mode}-${width}.png` })
      await row.focus()
      await page.keyboard.press('Enter')
      await expect(outputDetails(page).getByText('Ready · Revision 1 · CADCraft')).toBeVisible()
    })
  }
}

for (const mode of ['inline', 'thumbnails']) {
  test(`${mode} rows truncate long titles at 150% interface size on a narrow phone`, async ({ page, context }) => {
    const state = await fixture(context, { inline: true })
    const title = 'Actual app screenshots — ' + 'very-long-unbroken-filename'.repeat(12)
    state.chats.alpha.messages[0].meta.outputs[0].title = title
    await context.addInitScript(({ key, mode }) => localStorage.setItem(key, JSON.stringify({ outputDisplay: mode, interfaceSize: 150 })), { key: PREFERENCES_KEY, mode })
    await page.setViewportSize({ width: 320, height: 900 })
    await page.goto('/chats/alpha')
    const row = page.getByRole('button', { name: `View output: ${title}`, exact: true })
    await expect(row).toBeVisible()
    await expect(row).toHaveAttribute('title', `${title} · Revision 1`)
    expect(await row.locator('.output-row__title').evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)
    expect((await row.boundingBox()).height).toBeLessThan(90)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}

test('output display switches existing rows live across tabs and resets to inline', async ({ page, context }) => {
  const state = await fixture(context, { inline: true })
  await page.goto('/chats/alpha')
  const row = page.locator('.output-row')
  await expect(row).toBeVisible()
  expect(state.files).toHaveLength(0)
  const settings = await context.newPage()
  await settings.goto('/settings/workspace')
  const select = settings.getByRole('combobox', { name: 'Output display', exact: true })
  await select.selectOption('thumbnails')
  await expect(row.locator('img')).toBeVisible()
  await select.selectOption('inline')
  await expect(row.locator('.output-image')).toHaveCount(0)
  await select.selectOption('thumbnails')
  await expect(row.locator('img')).toBeVisible()
  await settings.getByRole('button', { name: 'Reset workspace', exact: true }).click()
  await expect(select).toHaveValue('inline')
  await expect(row.locator('.output-image')).toHaveCount(0)
  await settings.close()
})

test('thumbnail rows fall back quietly for missing or failed previews without blocking output access', async ({ page, context }) => {
  const state = await fixture(context, { inline: true })
  state.failPreview = true
  state.chats.alpha.messages[0].meta.outputs.push(output(1, { id: 'b'.repeat(32), title: 'Source only', files: [output().files[2]] }))
  await context.addInitScript(key => localStorage.setItem(key, JSON.stringify({ outputDisplay: 'thumbnails' })), PREFERENCES_KEY)
  await page.goto('/chats/alpha')
  const rows = page.locator('.output-row')
  await expect(rows).toHaveCount(2)
  await expect.poll(() => state.files.length).toBe(1)
  await expect(rows.locator('img')).toHaveCount(0)
  await expect(rows.getByRole('status')).toHaveCount(0)
  await expect(rows.getByRole('button')).toHaveCount(0)
  await expect(rows.nth(1).locator('.output-row__icon')).toBeVisible()
  await page.getByRole('button', { name: 'View output: Mounting bracket', exact: true }).click()
  const detail = outputDetails(page)
  await expect(detail.getByText(/Could not load preview/)).toBeVisible()
  state.failPreview = false
  await detail.getByRole('button', { name: 'Retry preview', exact: true }).click()
  await expect(detail.locator('img')).toBeVisible()
})

test('switching off thumbnails cancels in-flight preview work without a late object URL', async ({ page, context }) => {
  const state = await fixture(context, { inline: true })
  let release
  state.waitFile = new Promise(resolve => { release = resolve })
  await context.addInitScript(() => {
    const create = URL.createObjectURL.bind(URL)
    window.outputBlobs = 0
    URL.createObjectURL = (...args) => { window.outputBlobs++; return create(...args) }
  })
  await page.goto('/settings/workspace')
  await page.getByRole('combobox', { name: 'Output display', exact: true }).selectOption('thumbnails')
  await page.goto('/chats/alpha')
  await expect.poll(() => state.files.length).toBe(1)
  const settings = await context.newPage()
  await settings.goto('/settings/workspace')
  await settings.getByRole('combobox', { name: 'Output display', exact: true }).selectOption('inline')
  await expect(page.locator('.output-row .output-image')).toHaveCount(0)
  release()
  await expect.poll(() => state.completedFiles).toBe(1)
  expect(await page.evaluate(() => window.outputBlobs)).toBe(0)
  await settings.close()
})

test('Android saves authenticated output bytes with the native picker and waits for completion', async ({ page, context }) => {
  const state = await fixture(context)
  await mockNativeDownloads(context)
  await page.setViewportSize({ width: 390, height: 900 })
  let browserDownloads = 0
  page.on('download', () => browserDownloads++)
  await page.goto('/chats/alpha')
  const panel = await openBracket(page)
  const download = panel.getByRole('button', { name: 'Download bracket.pdf', exact: true })
  await download.click()
  await expect.poll(() => page.evaluate(() => window.nativeSaves)).toEqual([
    { id: 'staged-download', name: 'bracket.pdf', mimeType: 'application/pdf' }
  ])
  expect(await page.evaluate(() => btoa(window.stagedBytes))).toBe(pdf.toString('base64'))
  await expect(download).toBeDisabled()
  await expect(panel.getByRole('status')).toHaveCount(0)
  await page.evaluate(() => window.finishSave({ cancelled: false }))
  await expect(panel.getByRole('status')).toHaveText('Saved bracket.pdf to your selected location.')
  await expect(download).toBeEnabled()
  expect(state.files.find(file => !file.preview).authorization).toBe('Bearer fixture-native-access')
  // Editable source and preview files must use the same path, not navigate/open in WebView.
  await page.evaluate(() => { window.saveOutcome = 'saved' })
  await panel.getByRole('button', { name: 'Download bracket.dxf', exact: true }).click()
  await expect(panel.getByRole('status')).toContainText('Saved bracket.dxf')
  await panel.getByRole('button', { name: 'Download preview.png', exact: true }).click()
  await expect(panel.getByRole('status')).toContainText('Saved preview.png')
  expect(await page.evaluate(() => btoa(window.stagedBytes))).toBe(png.toString('base64'))
  expect((await page.evaluate(() => window.nativeSaves)).map(file => file.mimeType)).toEqual(['application/pdf', 'image/vnd.dxf', 'image/png'])
  expect(browserDownloads).toBe(0)
})

test('Android stages large binary downloads in bounded ordered chunks, keeping bytes out of picker state', async ({ page, context }) => {
  const state = await fixture(context)
  // Includes every byte value, multiple full chunks, and a non-aligned tail.
  state.downloadBytes = Buffer.alloc(2 * 1024 * 1024 + 17)
  for (let i = 0; i < state.downloadBytes.length; i++) state.downloadBytes[i] = i % 256
  await mockNativeDownloads(context)
  await page.goto('/chats/alpha')
  const panel = await openBracket(page)
  await panel.getByRole('button', { name: 'Download bracket.pdf', exact: true }).click()
  await expect.poll(() => page.evaluate(() => window.nativeSaves.length)).toBe(1)
  const chunks = await page.evaluate(() => window.nativeChunks)
  expect(chunks.length).toBeGreaterThan(10)
  expect(chunks.every(chunk => chunk.base64.length <= 256 * 1024)).toBe(true)
  expect(Buffer.concat(chunks.map(chunk => Buffer.from(chunk.base64, 'base64')))).toEqual(state.downloadBytes)
  const saves = await page.evaluate(() => window.nativeSaves)
  expect(JSON.stringify(saves).length).toBeLessThan(200)
  expect(saves[0]).not.toHaveProperty('base64')
  await page.evaluate(() => window.finishSave({ cancelled: false }))
  await expect.poll(() => page.evaluate(() => window.nativeDiscards)).toEqual(['staged-download'])
})

test('Android staging failures clean up without opening the picker and allow retry', async ({ page, context }) => {
  await fixture(context)
  await mockNativeDownloads(context)
  await page.goto('/chats/alpha')
  const panel = await openBracket(page)
  await page.evaluate(() => { window.failChunk = true })
  const download = panel.getByRole('button', { name: 'Download bracket.pdf', exact: true })
  await download.click()
  await expect(panel.getByRole('alert')).toContainText('Temporary storage is full')
  expect(await page.evaluate(() => window.nativeSaves)).toEqual([])
  expect(await page.evaluate(() => window.nativeDiscards)).toEqual(['staged-download'])
  await page.evaluate(() => { window.failChunk = false; window.saveOutcome = 'saved' })
  await download.click()
  await expect(panel.getByRole('status')).toContainText('Saved bracket.pdf')
})

test('Android navigation during staging discards bytes without launching a late picker', async ({ page, context }) => {
  await fixture(context)
  await mockNativeDownloads(context)
  await page.goto('/chats/beta')
  await page.locator('#shell-sidebar a[href="/chats/alpha"]').click()
  const panel = await openBracket(page)
  await page.evaluate(() => { window.pauseChunk = true })
  await panel.getByRole('button', { name: 'Download bracket.pdf', exact: true }).click()
  await expect.poll(() => page.evaluate(() => window.nativeChunks.length)).toBe(1)
  await page.goBack()
  await expect(page).toHaveURL(/\/chats\/beta$/)
  await expect(panel).toHaveCount(0)
  await page.evaluate(() => window.finishChunk())
  await expect.poll(() => page.evaluate(() => window.nativeDiscards)).toEqual(['staged-download'])
  expect(await page.evaluate(() => window.nativeSaves)).toEqual([])
})

test('older Android save plugins request an app update instead of using the unsafe transport', async ({ page, context }) => {
  await fixture(context)
  await mockNativeDownloads(context, { legacy: true })
  await page.goto('/chats/alpha')
  const panel = await openBracket(page)
  await panel.getByRole('button', { name: 'Download bracket.pdf', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('Update the Android app')
  expect(await page.evaluate(() => window.nativeSaves)).toEqual([])
})

test('Android cancellation and write errors are visible and allow retry', async ({ page, context }) => {
  await fixture(context)
  await mockNativeDownloads(context)
  await page.goto('/chats/alpha')
  const panel = await openBracket(page)
  const download = panel.getByRole('button', { name: 'Download bracket.pdf', exact: true })
  await page.evaluate(() => { window.saveOutcome = 'cancelled' })
  await download.click()
  await expect(panel.getByRole('status')).toHaveText('Save cancelled. No file was downloaded.')
  await page.evaluate(() => { window.saveOutcome = 'failed' })
  await download.click()
  await expect(panel.getByRole('alert')).toContainText('Storage is full')
  await expect(panel.getByRole('status')).toHaveCount(0)
  await expect(download).toBeEnabled()
  await page.evaluate(() => { window.saveOutcome = 'saved' })
  await download.click()
  await expect(panel.getByRole('status')).toContainText('Saved bracket.pdf')
  await expect(panel.getByRole('alert')).toHaveCount(0)
})

test('Android without the save plugin reports an error rather than silently using a blob link', async ({ page, context }) => {
  await fixture(context)
  await mockNativeDownloads(context, { available: false })
  await page.goto('/chats/alpha')
  const panel = await openBracket(page)
  await panel.getByRole('button', { name: 'Download bracket.pdf', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('Could not download file')
  await expect(panel.getByRole('status')).toHaveCount(0)
})

test('empty state and new publications do not steal chat focus', async ({ page, context }) => {
  const state = await fixture(context, { empty: true })
  await page.goto('/chats/alpha')
  await outputsTab(page).click()
  await expect(page.getByRole('heading', { name: 'No outputs yet' })).toBeVisible()
  await page.getByRole('tab', { name: 'Chat', exact: true }).click()
  const composer = page.getByRole('textbox', { name: 'Message', exact: true })
  await composer.fill('Still typing')
  state.chats.alpha.outputs = [output()]
  await expect(outputsTab(page)).toContainText('1')
  await expect(page.getByRole('tab', { name: 'Chat', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(composer).toBeFocused()
  expect(state.files).toHaveLength(0)
})

test('committed revisions update live, history stays immutable, selected output stays pinned', async ({ page, context }) => {
  const state = await fixture(context)
  await page.goto('/chats/alpha')
  const panel = await openBracket(page)
  state.chats.alpha.outputs = [output(2)]
  state.history.unshift(output(2))
  await expect(panel.getByText('Ready · Revision 2 · CADCraft')).toBeVisible()
  await expect.poll(() => state.files.some(file => file.path.includes('/revisions/2/'))).toBe(true)
  state.failHistory = true
  await panel.getByRole('button', { name: 'Load revision history' }).click()
  await expect(panel.getByRole('alert')).toContainText('History unavailable')
  state.failHistory = false
  await panel.getByRole('button', { name: 'Load revision history' }).click()
  await panel.getByRole('combobox', { name: 'Revision', exact: true }).selectOption({ label: 'Revision 1' })
  await expect(panel.getByText('Ready · Revision 1 · CADCraft')).toBeVisible()
  state.chats.alpha.outputs = [output(1, { id: 'c'.repeat(32), title: 'Other document' }), output(3)]
  await expect(outputsTab(page)).toContainText('2')
  await expect(panel.getByRole('heading', { name: 'Mounting bracket' })).toBeVisible()
  await expect(panel.getByText('Ready · Revision 1 · CADCraft')).toBeVisible()
  await panel.getByRole('combobox', { name: 'Revision', exact: true }).selectOption({ label: 'Latest · 3' })
  await expect(panel.getByText('Ready · Revision 3 · CADCraft')).toBeVisible()
})

test('storage cleanup removes stale reply cards and falls back from a deleted history selection', async ({ page, context }) => {
  const state = await fixture(context, { inline: true })
  state.chats.alpha.outputs = [output(2)]
  state.history.unshift(output(2))
  await page.goto('/chats/alpha')
  await page.getByRole('button', { name: 'View output: Mounting bracket', exact: true }).click()
  const panel = outputDetails(page)
  await expect(panel.getByText('Ready · Revision 1 · CADCraft')).toBeVisible()
  state.history = [output(2)]
  state.chats.alpha.messages[0].meta.outputs = []
  state.events.push({ kind: 'chat.output.deleted', chat_id: 'alpha' })
  await expect(panel.getByText('Ready · Revision 2 · CADCraft')).toBeVisible()
  await expect(panel.getByRole('alert')).toContainText('That revision was removed in Storage')
  await expect(panel.getByRole('combobox', { name: 'Revision', exact: true })).not.toContainText('Revision 1')
  await panel.getByRole('button', { name: 'All outputs', exact: true }).click()
  await expect(panel).toHaveCount(0)
  await page.getByRole('tab', { name: 'Chat', exact: true }).click()
  await expect(page.getByRole('button', { name: 'View output: Mounting bracket', exact: true })).toHaveCount(0)
  await outputsTab(page).click()
  state.chats.alpha.outputs = []
  state.history = []
  state.events.push({ kind: 'chat.output.deleted', chat_id: 'alpha' })
  await expect(page.getByRole('heading', { name: 'No outputs yet' })).toBeVisible()
})

test('viewing outputs does not acknowledge hidden replies', async ({ page, context }) => {
  const state = await fixture(context)
  await page.goto('/chats/alpha')
  await expect.poll(() => state.reads.some(read => read.message_id === 'reply')).toBe(true)
  await outputsTab(page).click()
  state.chats.alpha.messages.push({ id: 'unread-reply', role: 'assistant', content: 'A new response', meta: {} })
  state.chats.alpha.outputs = [output(2)]
  await expect(outputPanel(page).getByRole('button', { name: 'Open Mounting bracket', exact: true })).toContainText('Revision 2')
  expect(state.reads.some(read => read.message_id === 'unread-reply')).toBe(false)
  await page.getByRole('tab', { name: 'Chat', exact: true }).click()
  await expect(page.getByText('A new response', { exact: true })).toBeVisible()
  await expect.poll(() => state.reads.some(read => read.message_id === 'unread-reply')).toBe(true)
})

test('inline result card opens Outputs and image/download errors are retryable', async ({ page, context }) => {
  const state = await fixture(context, { inline: true })
  state.failPreview = true
  await page.goto('/chats/alpha')
  await page.getByRole('button', { name: 'View output: Mounting bracket', exact: true }).click()
  const panel = outputDetails(page)
  await expect(panel.getByText(/Could not load preview/)).toBeVisible()
  state.failPreview = false
  await panel.getByRole('button', { name: 'Retry preview', exact: true }).click()
  await expect(panel.locator('img')).toBeVisible()
  state.failDownload = true
  await panel.getByRole('button', { name: 'Download bracket.pdf', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('Could not download file')
  state.failDownload = false
  const downloaded = page.waitForEvent('download')
  await panel.getByRole('button', { name: 'Download bracket.pdf', exact: true }).click()
  await downloaded
  await expect(panel.getByRole('alert')).toHaveCount(0)
})

test('steered answers do not repeat already-saved outputs in the live response', async ({ page, context }) => {
  const state = await fixture(context, { inline: true })
  state.chats.alpha.active_turn = { id: 'turn', steps: [{ kind: 'text', text: 'Making the requested changes.' }] }
  await page.goto('/chats/alpha')
  const cards = page.getByRole('button', { name: 'View output: Mounting bracket', exact: true })
  await expect(page.getByText('Making the requested changes.', { exact: true })).toBeVisible()
  await expect(cards).toHaveCount(1)
  state.chats.alpha.outputs = [output(2)]
  await expect(cards).toHaveCount(2)
  await expect(cards.nth(0)).toContainText('rev 1')
  await expect(cards.nth(1)).toContainText('rev 2')
})

test('older reply cards open their exact revision while the gallery follows the latest', async ({ page, context }) => {
  const state = await fixture(context, { inline: true })
  state.chats.alpha.outputs = [output(2)]
  state.history.unshift(output(2))
  await page.goto('/chats/alpha')
  await page.getByRole('button', { name: 'View output: Mounting bracket', exact: true }).click()
  const panel = outputDetails(page)
  await expect(panel.getByText('Ready · Revision 1 · CADCraft')).toBeVisible()
  const downloaded = page.waitForEvent('download')
  await panel.getByRole('button', { name: 'Download bracket.pdf', exact: true }).click()
  await downloaded
  expect(state.files.find(file => !file.preview).path).toContain('/revisions/1/')
  state.chats.alpha.outputs = [output(3)]
  state.history.unshift(output(3))
  await expect(panel.getByRole('combobox', { name: 'Revision', exact: true })).toContainText('Latest · 3')
  await expect(panel.getByText('Ready · Revision 1 · CADCraft')).toBeVisible()
  await panel.getByRole('combobox', { name: 'Revision', exact: true }).selectOption({ label: 'Latest · 3' })
  await expect(panel.getByText('Ready · Revision 3 · CADCraft')).toBeVisible()
})

test('switching chats cancels a pending preview without leaking a late object URL', async ({ page, context }) => {
  const state = await fixture(context)
  let release
  state.waitFile = new Promise(resolve => { release = resolve })
  await context.addInitScript(() => {
    const create = URL.createObjectURL.bind(URL)
    window.outputBlobs = 0
    URL.createObjectURL = (...args) => { window.outputBlobs++; return create(...args) }
  })
  await page.goto('/chats/alpha')
  await outputsTab(page).click()
  await expect.poll(() => state.files.length).toBe(1)
  await page.locator('#shell-sidebar a[href="/chats/beta"]').click()
  await outputsTab(page).click()
  await expect(page.getByRole('heading', { name: 'No outputs yet' })).toBeVisible()
  release()
  await expect.poll(() => state.completedFiles).toBe(1)
  expect(await page.evaluate(() => window.outputBlobs)).toBe(0)
  await expect(outputPanel(page).locator('img')).toHaveCount(0)
  await page.locator('#shell-sidebar a[href="/chats/alpha"]').click()
  await outputsTab(page).click()
  await expect(outputPanel(page).locator('img')).toBeVisible()
  expect(await page.evaluate(() => window.outputBlobs)).toBe(1)
})

test('tab controls support keyboard navigation and route changes discard old outputs', async ({ page, context }) => {
  await fixture(context)
  await page.goto('/chats/alpha')
  const chatTab = page.getByRole('tab', { name: 'Chat', exact: true })
  await chatTab.focus()
  await page.keyboard.press('ArrowRight')
  await expect(outputsTab(page)).toBeFocused()
  await expect(outputPanel(page)).toBeVisible()
  await page.keyboard.press('Home')
  await expect(chatTab).toBeFocused()
  await page.keyboard.press('End')
  await expect(outputsTab(page)).toBeFocused()
  await page.locator('#shell-sidebar a[href="/chats/beta"]').click()
  await expect(chatTab).toHaveAttribute('aria-selected', 'true')
  await outputsTab(page).click()
  await expect(page.getByRole('heading', { name: 'No outputs yet' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Mounting bracket' })).toHaveCount(0)
})

const asset = (id, name, mime_type, extras = {}) => ({ id, name, mime_type, size: 100, role: 'file', previewable: false, ...extras })

test('mobile Workspace selects distinct outputs, their revisions, and files within a revision', async ({ page, context }) => {
  const state = await fixture(context, { inline: true })
  const firstFiles = [asset('draft', 'notes.md', 'text/markdown')]
  const currentFiles = [asset('latest', 'notes.md', 'text/markdown'), asset('original', 'archive.zip', 'application/zip')]
  const oldest = output(1, { title: 'Launch notes', files: firstFiles })
  state.chats.alpha.messages[0].meta.outputs = [oldest]
  state.chats.alpha.outputs = [output(2, { title: 'Launch notes', files: currentFiles }), output(1, { id: 'b'.repeat(32), title: 'Station notes', files: [asset('station-notes', 'notes.md', 'text/markdown')] })]
  state.history = [state.chats.alpha.outputs[0], oldest]
  state.fileBodies.set('draft', '# Original notes')
  state.fileBodies.set('latest', '# Updated notes')
  state.fileBodies.set('station-notes', '# Independent notes')
  await page.setViewportSize({ width: 320, height: 750 })
  await page.goto('/chats/alpha')
  await page.getByRole('button', { name: 'View output: Launch notes' }).click()
  const panel = outputDetails(page)
  const revision = panel.getByRole('combobox', { name: 'Revision' })
  await expect(revision).toHaveValue('1')
  await panel.getByRole('button', { name: 'Open notes.md' }).click()
  const viewer = fileDialog(page)
  await expect(viewer.getByRole('heading', { name: 'Original notes' })).toBeVisible()
  await viewer.getByRole('button', { name: 'Source' }).click()
  await expect(viewer.locator('pre')).toHaveText('# Original notes')
  await viewer.getByRole('button', { name: 'Close file viewer' }).click()
  await revision.selectOption({ label: 'Latest · 2' })
  await panel.getByRole('button', { name: 'Open notes.md' }).click()
  await expect(viewer.getByRole('heading', { name: 'Updated notes' })).toBeVisible()
  await viewer.getByRole('combobox', { name: 'File' }).selectOption('original')
  await expect(viewer.getByRole('heading', { name: 'No preview available' })).toBeVisible()
  await expect(viewer.getByRole('button', { name: 'Download file', exact: true })).toBeVisible()
  expect(state.files.some(file => file.path.includes('/files/original'))).toBe(false)
  await viewer.getByRole('button', { name: 'Close file viewer' }).click()
  await panel.getByRole('button', { name: 'All outputs', exact: true }).click()
  const launch = outputPanel(page).getByRole('button', { name: 'Open Launch notes', exact: true })
  const station = outputPanel(page).getByRole('button', { name: 'Open Station notes', exact: true })
  await expect(launch).toBeFocused()
  expect((await launch.boundingBox()).x).toBe((await station.boundingBox()).x)
  await page.screenshot({ path: 'test-results/workspace-mobile-gallery.png', animations: 'disabled' })
  await station.click()
  await panel.getByRole('button', { name: 'Open notes.md' }).click()
  await expect(viewer.getByRole('heading', { name: 'Independent notes' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('gallery lays out previews and compact files without opening a detail by default', async ({ page, context }) => {
  const state = await fixture(context)
  state.chats.alpha.outputs.push(output(2, { id: 'b'.repeat(32), title: 'Station signs' }),
    output(1, { id: 'c'.repeat(32), title: 'Station assets', files: [asset('zip', 'assets.zip', 'application/zip')] }))
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/chats/alpha')
  await outputsTab(page).click()
  const gallery = outputPanel(page).getByRole('list', { name: 'Published outputs', exact: true })
  const bracket = gallery.getByRole('button', { name: 'Open Mounting bracket', exact: true })
  const signs = gallery.getByRole('button', { name: 'Open Station signs', exact: true })
  const assets = gallery.getByRole('button', { name: 'Open Station assets', exact: true })
  await expect(gallery.locator('img')).toHaveCount(2)
  await expect(assets).toContainText('assets.zip')
  await expect(assets.locator('img')).toHaveCount(0)
  expect((await signs.boundingBox()).x).toBeGreaterThan((await bracket.boundingBox()).x)
  expect((await assets.boundingBox()).height).toBeLessThan((await bracket.boundingBox()).height)
  await expect(outputDetails(page)).toHaveCount(0)
  await expect(outputPanel(page).getByRole('button', { name: /canvas|browse outputs/i })).toHaveCount(0)
  expect(state.files).toHaveLength(2)
  expect(state.files.every(file => file.preview && file.path.includes('/files/preview-'))).toBe(true)
  await page.screenshot({ path: 'test-results/workspace-desktop-gallery.png' })
})

test('gallery preserves scroll and keyboard focus when dismissing a detail', async ({ page, context }) => {
  const state = await fixture(context)
  state.chats.alpha.outputs.push(...Array.from({ length: 24 }, (_, i) => output(1, {
    id: String(i).padStart(32, '0'), title: `Station ${i}`, files: [asset(`file-${i}`, `station-${i}.zip`, 'application/zip')]
  })))
  await page.setViewportSize({ width: 390, height: 720 })
  await page.goto('/chats/alpha')
  await outputsTab(page).click()
  const panel = outputPanel(page)
  const tile = panel.getByRole('button', { name: 'Open Station 23', exact: true })
  await tile.scrollIntoViewIfNeeded()
  const scrollTop = () => panel.evaluate(el => el.scrollTop)
  const before = await scrollTop()
  expect(before).toBeGreaterThan(0)
  await tile.press('Enter')
  await expect(outputDetails(page).getByRole('heading', { name: 'Station 23' })).toBeVisible()
  await expect(outputDetails(page).getByRole('button', { name: 'All outputs', exact: true })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(tile).toBeFocused()
  expect(await scrollTop()).toBe(before)
})

function pdfWithTwoPages () {
  const paint = '1 0 0 rg 10 10 140 140 re f'
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 160 160] /Resources << >> /Contents 5 0 R >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 160 160] /Resources << >> /Contents 5 0 R >>',
    `<< /Length ${paint.length} >>\nstream\n${paint}\nendstream`
  ]
  let content = '%PDF-1.4\n'
  const offsets = objects.map((object, i) => { const at = content.length; content += `${i + 1} 0 obj\n${object}\nendobj\n`; return at })
  return content + `xref\n0 6\n0000000000 65535 f \n${offsets.map(at => `${String(at).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${content.length}\n%%EOF\n`
}

test('file viewer isolates HTML and opens PDF, original images and published slide images', async ({ page, context }) => {
  const state = await fixture(context)
  state.chats.alpha.outputs = [output(1, { files: [
    asset('html', 'launch.html', 'text/html'), asset('real-pdf', 'guide.pdf', 'application/pdf'), asset('image', 'photo.png', 'image/png'),
    asset('deck', 'briefing.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
    asset('slide-a', 'slide-a.png', 'image/png', { role: 'preview', previewable: true }), asset('slide-b', 'slide-b.png', 'image/png', { role: 'preview', previewable: true })
  ] })]
  state.fileBodies.set('html', '<!doctype html><html><head><style>h1 {color: rgb(40, 50, 60)}</style></head><body><h1>Launch microsite</h1><a href="https://example.invalid/leave">External link</a><script>document.body.textContent = "blocked script"</script></body></html>')
  state.fileBodies.set('real-pdf', pdfWithTwoPages())
  state.fileBodies.set('image', png)
  await page.goto('/chats/alpha')
  const panel = await openBracket(page)
  await panel.getByRole('button', { name: 'Open launch.html' }).click()
  const dialog = fileDialog(page)
  const frame = dialog.frameLocator('iframe')
  const select = dialog.getByRole('combobox', { name: 'File' })
  await expect(dialog.locator('iframe')).toHaveAttribute('sandbox', '')
  await expect(frame.getByRole('heading', { name: 'Launch microsite' })).toHaveCSS('color', 'rgb(40, 50, 60)')
  await expect(frame.getByText('External link')).not.toHaveAttribute('href')
  await dialog.getByRole('button', { name: 'Phone' }).click()
  expect((await dialog.locator('iframe').boundingBox()).width).toBeLessThanOrEqual(390)
  await select.selectOption('real-pdf')
  const renderedPage = dialog.getByRole('img', { name: 'Page 1 of 2' })
  await expect(renderedPage).toBeVisible()
  await expect.poll(() => renderedPage.evaluate(node => [...node.getContext('2d').getImageData(node.width / 2, node.height / 2, 1, 1).data])).toEqual([255, 0, 0, 255])
  await dialog.getByRole('button', { name: 'Next page' }).click()
  await expect(dialog.getByRole('img', { name: 'Page 2 of 2' })).toBeVisible()
  await select.selectOption('image')
  await expect(dialog.getByRole('img', { name: 'photo.png' })).toBeVisible()
  await dialog.getByRole('button', { name: '100%' }).click()
  await expect(dialog.getByRole('button', { name: '100%' })).toHaveAttribute('aria-pressed', 'true')
  await select.selectOption('deck')
  await expect(dialog.getByRole('img', { name: 'Slide 1' })).toBeVisible()
  await dialog.getByRole('button', { name: 'Present' }).click()
  await page.keyboard.press('ArrowRight')
  await expect(dialog.getByRole('img', { name: 'Slide 2' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog.getByRole('button', { name: 'Present', exact: true })).toBeVisible()
})
