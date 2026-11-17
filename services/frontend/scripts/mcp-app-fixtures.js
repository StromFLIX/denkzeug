/** Real SDK fixtures shared by native-origin integration checks; no backend credentials. */
import { rolldown } from 'rolldown'
import { fileURLToPath } from 'node:url'

async function bundle (path, name) {
  const build = await rolldown({ input: fileURLToPath(new URL(path, import.meta.url)) })
  try {
    const { output } = await build.generate({ format: 'iife', minify: true, ...(name ? { name } : {}) })
    return output[0].code
  } finally { await build.close() }
}

export async function nativeAppFixtures () {
  const host = await bundle('../src/mcp-apps.js', 'DenkzeugMcpHostTest')
  const client = await bundle('../tests/fixtures/mcp-app-client.js')
  const html = `<!doctype html><html><head><script>window.firstSurface = { scheme: getComputedStyle(document.documentElement).colorScheme, background: getComputedStyle(document.documentElement).backgroundColor }</script></head><body><h2 id="result">Waiting</h2><button id="copy">Copy Markdown</button><button id="link">Open release page</button><p id="status"></p><script>${client.replace(/<\/script/gi, '<\\/script')}</script></body></html>`
  return { host, html }
}
