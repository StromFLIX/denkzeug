import assert from 'node:assert/strict'
import { test } from 'node:test'
import picomatch from 'picomatch'
import { onlyMitigatedPicomatchFindings } from './audit-dependencies.mjs'
import { verifyPicomatchMitigation } from './picomatch-mitigation.mjs'

function report () {
  return { auditReportVersion: 2, vulnerabilities: {
    picomatch: {
      nodes: ['node_modules/picomatch'],
      via: [{ dependency: 'picomatch', url: 'https://github.com/advisories/GHSA-fc5q-39xc-vjpf', range: '<=4.0.7' }]
    },
    tinyglobby: { via: ['picomatch', 'fdir'] },
    fdir: { via: ['tinyglobby'] }
  } }
}

test('install verifies the guarded parser and preserves ordinary glob matching', () => {
  assert.ok(verifyPicomatchMitigation())
  assert.ok(picomatch('**/*.{js,ts}')('src/app.ts'))
  assert.ok(picomatch('+(ab)')('abab'))
  assert.ok(picomatch('+(a|b)')('abba'))
  for (const operator of ['+', '*']) {
    const pattern = `${operator}(a[bc])`
    const match = picomatch(pattern, { maxExtglobRecursion: false })
    assert.ok(match(pattern))
    assert.ok(!match('abac'))
  }
})

test('the audit exception follows only the verified advisory through transitive findings', () => {
  assert.ok(onlyMitigatedPicomatchFindings(report()))
  const anotherCopy = report()
  anotherCopy.vulnerabilities.picomatch.nodes.push('node_modules/example/node_modules/picomatch')
  assert.ok(!onlyMitigatedPicomatchFindings(anotherCopy))
  const anotherAdvisory = report()
  anotherAdvisory.vulnerabilities.picomatch.via.push({ dependency: 'picomatch', url: 'https://example.test/another-advisory' })
  assert.ok(!onlyMitigatedPicomatchFindings(anotherAdvisory))
  const unrelated = report()
  unrelated.vulnerabilities.other = { via: ['other'] }
  assert.ok(!onlyMitigatedPicomatchFindings(unrelated))
  assert.ok(!onlyMitigatedPicomatchFindings({ ...report(), error: { code: 'ENOAUDIT' } }))
})
