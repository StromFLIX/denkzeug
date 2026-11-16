#!/usr/bin/env node
import { chromium, expect } from '@playwright/test'
import { createServer } from 'vite'
import { mkdir, writeFile } from 'node:fs/promises'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { mockWebsiteProduct, captureTime, chatPath } from '../tests/website-product-fixture.js'

// Browser pixels only: no CSS injection, DOM alteration, resizing or compositing.
const frontendRoot = fileURLToPath(new URL('../', import.meta.url))
const destination = fileURLToPath(new URL('../../website/assets/product-tour/', import.meta.url))
const dataPath = fileURLToPath(new URL('../../website/product-tour-data.mjs', import.meta.url))
const staticDestination = fileURLToPath(new URL('../../website/assets/product/', import.meta.url))
const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: frontendRoot, encoding: 'utf8' }).trim()
await mkdir(destination, { recursive: true })
const server = await createServer({ root: frontendRoot, server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const origin = server.resolvedUrls.local[0].replace(/\/$/, '')
let browser
const scenes = [], captures = [], staticCaptures = [], errors = [], forbidden = []
try {
  browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1280, height: 840 }, deviceScaleFactor: 1.5, locale: 'en-US', timezoneId: 'UTC', colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block' })
  const page = await context.newPage()
  await page.clock.setFixedTime(captureTime)
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  page.on('response', response => { if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`) })
  const fixture = await mockWebsiteProduct(context)
  await context.route('**/*', route => {
    const request = route.request(), url = new URL(request.url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname) || (url.pathname.startsWith('/api/') && !['GET', 'HEAD'].includes(request.method()) && !url.pathname.endsWith('/read-state'))) {
      forbidden.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    }
    return route.fallback()
  })
  const nav = label => page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('link', { name: label, exact: true })
  async function capture (id, title, additional = []) {
    await page.mouse.move(1279, 839)
    await page.evaluate(() => document.fonts.ready)
    await page.waitForTimeout(700)
    await expect(page.locator('html')).toHaveCSS('font-size', '20px')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'denkzeug')
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText(/Draft sync unavailable|Connecting|Reconnecting|Loading…|Something went wrong/)).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(await page.evaluate(() => [...document.images].every(image => image.complete && image.naturalWidth > 0))).toBe(true)
    expect(errors).toEqual([]); expect(forbidden).toEqual([])
    expect(fixture.state.sent).toEqual([]); expect(fixture.state.writes).toEqual([])
    const hotspots = [], controls = []
    const targets = [['chat', 'Chats'], ['workflows', 'Workflows'], ['runs', 'Runs'], ['settings', 'Settings']]
    for (const [target, label] of targets) {
      if (target === id || (id === 'statistics' && target === 'runs')) continue
      if (await nav(label).isVisible()) additional.push({ id: `nav-${target}`, label: `Show ${label} screenshot`, target, locator: nav(label), selector: `navigation[Main navigation] link[${label}]` })
    }
    for (const control of additional) {
      await expect(control.locator).toBeInViewport({ ratio: 1 })
      const box = await control.locator.evaluate(element => { const r = element.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height } })
      const rect = { x: box.x / 1280, y: box.y / 840, width: box.width / 1280, height: box.height / 840 }
      for (const value of Object.values(rect)) expect(value).toBeGreaterThanOrEqual(0)
      expect(rect.x + rect.width).toBeLessThanOrEqual(1); expect(rect.y + rect.height).toBeLessThanOrEqual(1)
      hotspots.push({ id: control.id, label: control.label, target: control.target, rect })
      controls.push({ id: control.id, selector: control.selector, pixels: box })
    }
    let previous, png
    for (let attempt = 0; attempt < 12; attempt++) {
      const frame = await page.screenshot({ animations: 'disabled', caret: 'hide' })
      if (previous?.equals(frame)) { png = frame; break }
      previous = frame
      await page.waitForTimeout(100)
    }
    expect(png, `Consecutive identical ${id} browser frames`).toBeTruthy()
    await writeFile(`${destination}${id}.png`, png)
    scenes.push({ id, title, src: `/assets/product-tour/${id}.webp`, alt: `Actual Denkzeug Vue app — ${title}, with fictional sample data`, hotspots, source: { route: new URL(page.url()).pathname, commit } })
    captures.push({ id, route: new URL(page.url()).pathname, controls, pngBytes: png.length })
  }
  await page.goto(`${origin}${chatPath}`)
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible()
  await expect(page.locator('.tool-group__label')).toHaveText('Ran 3 tool calls')
  await capture('chat', 'Chats', [{ id: 'tab-outputs', label: 'Show Outputs screenshot', target: 'outputs', locator: page.getByRole('tab', { name: /Outputs/ }), selector: 'tab[name=/Outputs/]' }])
  await page.getByRole('tab', { name: /Outputs/ }).click()
  await expect(page.getByRole('button', { name: 'Download product-brief.docx', exact: true })).toBeVisible()
  await capture('outputs', 'Outputs', [{ id: 'tab-chat', label: 'Show Chats screenshot', target: 'chat', locator: page.getByRole('tab', { name: 'Chat', exact: true }), selector: 'tab[name=Chat]' }])
  await nav('Workflows').click()
  await page.locator('a[href="/workflows/digest"]').click()
  await expect(page.locator('.flow__toolbar')).toBeVisible()
  await page.getByRole('button', { name: 'Fit all steps', exact: true }).click()
  await capture('workflows', 'Workflows')
  await page.getByRole('link', { name: 'All runs', exact: true }).click()
  await page.locator('a[href="/runs/digest-record"]').click()
  await page.getByRole('button', { name: 'Details', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Output', exact: true })).toBeVisible()
  await capture('runs', 'Runs', [{ id: 'run-statistics', label: 'Show Statistics screenshot', target: 'statistics', locator: page.getByRole('button', { name: 'Statistics', exact: true }), selector: 'button[name=Statistics]' }])
  await page.getByRole('button', { name: 'Statistics', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Run statistics' })).toBeVisible()
  await capture('statistics', 'Run statistics', [{ id: 'statistics-details', label: 'Show Runs screenshot', target: 'runs', locator: page.getByRole('button', { name: 'Details', exact: true }), selector: 'button[name=Details]' }])
  await nav('Settings').click()
  await page.getByRole('link', { name: 'General', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'General', exact: true })).toBeVisible()
  await capture('settings', 'Settings', [{ id: 'back-workspace', label: 'Show Statistics screenshot', target: 'statistics', locator: page.getByRole('button', { name: 'Back to workspace', exact: true }), selector: 'button[name="Back to workspace"]' }])
  for (const scene of scenes) for (const hotspot of scene.hotspots) expect(scenes.some(target => target.id === hotspot.target)).toBe(true)
  // Refresh fallback and phone screenshots too: they must not advertise removed UI.
  for (const [id, width, height, dpr] of [['desktop', 1280, 840, 1.5], ['desktop-small', 1280, 840, 1], ['phone', 390, 844, 2]]) {
    const device = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: dpr, locale: 'en-US', timezoneId: 'UTC', colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block' })
    const shot = await device.newPage()
    await shot.clock.setFixedTime(captureTime)
    shot.on('pageerror', error => errors.push(error.message))
    const sample = await mockWebsiteProduct(device)
    await device.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (!['127.0.0.1', 'localhost'].includes(url.hostname)) { forbidden.push(request.url()); return route.abort() }
      return route.fallback()
    })
    await shot.goto(`${origin}${chatPath}`)
    await expect(shot.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible()
    if (id === 'phone') await shot.getByRole('tab', { name: /Outputs/ }).click()
    await shot.evaluate(() => document.fonts.ready)
    await shot.waitForTimeout(700)
    await expect(shot.getByRole('link', { name: 'Apps', exact: true })).toHaveCount(0)
    expect(await shot.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    let previous, png
    for (let attempt = 0; attempt < 12; attempt++) {
      const frame = await shot.screenshot({ animations: 'disabled', caret: 'hide' })
      if (previous?.equals(frame)) { png = frame; break }
      previous = frame
      await shot.waitForTimeout(100)
    }
    expect(png, `Consecutive identical ${id} browser frames`).toBeTruthy()
    expect(sample.state.sent).toEqual([]); expect(sample.state.writes).toEqual([])
    await writeFile(`${staticDestination}${id}.png`, png)
    staticCaptures.push({ id, viewport: { width, height }, deviceScaleFactor: dpr, pngBytes: png.length, requests: sample.requests })
    await device.close()
  }
  expect(errors).toEqual([]); expect(forbidden).toEqual([])
  const conversions = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-c', `
import sys, json, hashlib
from pathlib import Path
from PIL import Image, features
results = []
for path in sorted(path for folder in sys.argv[1:] for path in Path(folder).glob('*.png')):
    original = Image.open(path).convert('RGB')
    target = path.with_suffix('.webp')
    original.save(target, 'WEBP', lossless=True, quality=100, method=6, exact=True)
    decoded = Image.open(target).convert('RGB')
    expected = {'phone': (780, 1688), 'desktop-small': (1280, 840)}.get(path.stem, (1920, 1260))
    assert original.size == expected
    assert decoded.tobytes() == original.tobytes(), path
    results.append(dict(id=path.stem, webpBytes=target.stat().st_size, pngSha256=hashlib.sha256(path.read_bytes()).hexdigest(), webpSha256=hashlib.sha256(target.read_bytes()).hexdigest(), pixelIdentical=True))
assert sum(item['webpBytes'] for item in results) < 1200000
print(json.dumps(dict(libwebp=features.version('webp'), files=results)))
`, destination, staticDestination], { encoding: 'utf8' }))
  await writeFile(dataPath, `// Generated by services/frontend/scripts/capture-website.mjs; actual UI, fictional data.\nexport const productTour = Object.freeze(${JSON.stringify({ initial: 'chat', viewport: { width: 1280, height: 840 }, scenes }, null, 2)});\n`)
  await writeFile(`${destination}capture-report.json`, JSON.stringify({ commit, clock: captureTime.toISOString(), viewport: { width: 1280, height: 840 }, deviceScaleFactor: 1.5, pixels: { width: 1920, height: 1260 }, errors, forbidden, captures, conversions, requests: fixture.requests }, null, 2) + '\n')
  await writeFile(`${staticDestination}capture-report.json`, JSON.stringify({ commit, clock: captureTime.toISOString(), errors, forbidden, captures: staticCaptures, conversions }, null, 2) + '\n')
  console.log(`Captured ${scenes.length} actual Vue scenes and ${staticCaptures.length} device views`)
  await context.close()
} finally {
  await browser?.close()
  await server.close()
}
