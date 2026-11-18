// Temporary mitigation for GHSA-fc5q-39xc-vjpf; upstream 4.0.7 is still affected.
// Once a patched version is locked, remove this script, its postinstall hook,
// Dockerfile COPY and tests, and restore the unwrapped npm audit CI step.
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const parser = new URL('../node_modules/picomatch/lib/parse.js', import.meta.url)
const manifest = new URL('../node_modules/picomatch/package.json', import.meta.url)
const originalHash = '4e1e05447668aa1e94366c5516a2bcd8f5fff800cc92b1d0cd7d77703b8618c4'
const fixedHash = '7069207d7351ccb5dc5ea4a7c24693ec0eacf29708eaa2c150b7c390e61954a4'
const digest = source => createHash('sha256').update(source).digest('hex')
const versionMatches = () => JSON.parse(readFileSync(manifest, 'utf8')).version === '4.0.7'

export function verifyPicomatchMitigation () {
  return versionMatches() && digest(readFileSync(parser)) === fixedHash
}

export function applyPicomatchMitigation () {
  if (!versionMatches()) throw new Error('Review the picomatch mitigation: unexpected upstream version.')
  const source = readFileSync(parser, 'utf8')
  if (digest(source) === fixedHash) return
  if (digest(source) !== originalHash) throw new Error('Review the picomatch mitigation: unexpected upstream parser.')
  const fixed = source.replace(
    "    if ((token.type === 'plus' || token.type === 'star') && analysis.risky) {",
    String.raw`    // Denkzeug backport guard (GHSA-fc5q-39xc-vjpf): only a simple literal
    // single branch may emit the unbounded group. The existing safe-output or
    // escaped-literal fallback handles other single-branch bodies.
    const isRepeated = token.type === 'plus' || token.type === 'star';
    if (isRepeated && (analysis.risky || (!/^[\w-]+$/.test(body) && splitTopLevel(body).length === 1))) {`
  )
  if (digest(fixed) !== fixedHash) throw new Error('Unexpected picomatch mitigation result.')
  writeFileSync(parser, fixed)
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) applyPicomatchMitigation()
