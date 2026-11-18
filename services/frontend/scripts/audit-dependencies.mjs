import { spawnSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import { verifyPicomatchMitigation } from './picomatch-mitigation.mjs'

const advisory = 'https://github.com/advisories/GHSA-fc5q-39xc-vjpf'

export function onlyMitigatedPicomatchFindings (report) {
  const findings = report?.vulnerabilities
  if (report?.error || report?.auditReportVersion !== 2 || !findings) return false
  const pico = findings.picomatch
  if (!pico || pico.nodes?.length !== 1 || pico.nodes[0] !== 'node_modules/picomatch') return false

  const dependents = new Map(Object.keys(findings).map(name => [name, []]))
  for (const [name, finding] of Object.entries(findings)) {
    if (!Array.isArray(finding.via) || !finding.via.length) return false
    for (const cause of finding.via) {
      if (typeof cause === 'string') {
        if (!dependents.has(cause)) return false
        dependents.get(cause).push(name)
      } else if (name !== 'picomatch' || cause?.dependency !== 'picomatch' || cause.url !== advisory || cause.range !== '<=4.0.7') {
        return false
      }
    }
  }
  if (!pico.via.some(cause => cause?.url === advisory)) return false

  // npm repeats a direct finding through its dependency graph. Trace those
  // reports back to the single mitigated package, including cyclic dependencies.
  const covered = new Set(['picomatch'])
  for (const name of covered) for (const dependent of dependents.get(name)) covered.add(dependent)
  return covered.size === dependents.size
}

function audit () {
  const result = spawnSync(process.execPath, [process.env.npm_execpath, 'audit', '--audit-level=moderate', '--json'], {
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'], maxBuffer: 8 * 1024 * 1024
  })
  if (result.error) throw result.error
  if (result.status === 0) {
    console.log(result.stdout)
    return
  }
  let report
  try { report = JSON.parse(result.stdout) } catch { /* leave audit failed */ }
  // npm audits the published version, not the parser installed by postinstall.
  // Accept this finding only when that exact installed parser passes verification.
  if (result.status === 1 && onlyMitigatedPicomatchFindings(report) && verifyPicomatchMitigation()) {
    console.log(`npm audit: only ${advisory} and dependent findings; verified the local parser mitigation.`)
    return
  }
  console.error(result.stdout || 'npm audit did not return a report.')
  process.exitCode = 1
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) audit()
