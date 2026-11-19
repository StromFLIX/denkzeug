import test from 'node:test'
import assert from 'node:assert/strict'
import { compactTokens, dateLabel, dateRange, share } from './usage.js'

test('unknown usage is a dash, zero is zero, and large counts retain units', () => {
  assert.equal(compactTokens(null), '—')
  assert.equal(compactTokens(0), '0')
  assert.equal(compactTokens(751605), '751.6K')
  assert.equal(compactTokens(24800000), '24.80M')
  assert.equal(compactTokens(1000000000), '1.00B')
  assert.equal(share(null, 10), '—')
  assert.equal(share(0, 10), '0.0%')
  assert.equal(share(5, 10), '50.0%')
})
test('presets use calendar days in the chosen timezone including month and year boundaries', () => {
  assert.deepEqual(dateRange(30, 'America/New_York', new Date('2027-01-01T02:00:00Z')), { start: '2026-12-02', end: '2026-12-31' })
  assert.deepEqual(dateRange(7, 'UTC', new Date('2027-01-01T02:00:00Z')), { start: '2026-12-26', end: '2027-01-01' })
})
test('daily labels use the bucket calendar date and can disambiguate years', () => {
  const date = '2026-11-01'
  const localFormat = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC' })
  assert.equal(dateLabel(date), localFormat.format(new Date(`${date}T00:00:00Z`)))
  assert.notEqual(dateLabel(date, true), dateLabel('2025-11-01', true))
  assert.equal(dateLabel('not-a-date'), '')
})
