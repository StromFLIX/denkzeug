import test from 'node:test'
import assert from 'node:assert/strict'
import { compactTokens, dateRange, chartBuckets, share } from './usage.js'

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
test('weekly and monthly buckets reconcile exactly with daily tokens', () => {
  const daily = ['2026-10-31', '2026-11-01', '2026-11-02'].map(date => ({ date, tokens: { total: 30 }, chats: { total: 20 }, workflows: { total: 10 }, executions: 1 }))
  for (const mode of ['day', 'week', 'month']) {
    const buckets = chartBuckets(daily, mode)
    assert.equal(buckets.reduce((sum, bucket) => sum + bucket.total, 0), 90)
    assert.equal(buckets.reduce((sum, bucket) => sum + bucket.chats + bucket.workflows, 0), 90)
  }
  assert.deepEqual(chartBuckets(daily, 'week').map(bucket => bucket.date), ['2026-10-26', '2026-11-02'])
  assert.deepEqual(chartBuckets(daily, 'month').map(bucket => bucket.date), ['2026-10-01', '2026-11-01'])
})
