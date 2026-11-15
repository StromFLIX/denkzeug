/** Token analytics presentation helpers; no estimates or provider calls. */
export const TOKEN_CATEGORIES = [
  { key: 'input', label: 'Uncached input', color: 'var(--accent)' },
  { key: 'output', label: 'Output', color: 'var(--text)' },
  { key: 'cacheRead', label: 'Cache read', color: 'var(--flow-signal)' },
  { key: 'cacheWrite', label: 'Cache write', color: 'color-mix(in srgb, var(--flow-signal) 45%, var(--surface-panel))' }
]
export const BREAKDOWNS = [
  ['models', 'Models'], ['agents', 'Agents'], ['workflows', 'Workflows'], ['users', 'Users'], ['executions', 'Executions']
]
export function compactTokens (value) {
  if (value == null) return '—'
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`
  return value.toLocaleString()
}
export function addDays (day, count) {
  const date = new Date(`${day}T12:00:00Z`)
  date.setUTCDate(date.getUTCDate() + count)
  return date.toISOString().slice(0, 10)
}
export function dateRange (days = 30, timezone = 'UTC', now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now)
  const part = key => parts.find(item => item.type === key).value
  const end = `${part('year')}-${part('month')}-${part('day')}`
  return { start: addDays(end, 1 - days), end }
}
export function chartBuckets (daily, mode = 'day') {
  const buckets = new Map()
  for (const day of daily) {
    const date = new Date(`${day.date}T12:00:00Z`)
    const key = mode === 'month' ? `${day.date.slice(0, 7)}-01` : mode === 'week' ? addDays(day.date, -((date.getUTCDay() + 6) % 7)) : day.date
    if (!buckets.has(key)) buckets.set(key, { date: key, end: day.date, chats: 0, workflows: 0, total: 0, executions: 0 })
    const bucket = buckets.get(key)
    bucket.end = day.date
    bucket.chats += day.chats?.total || 0
    bucket.workflows += day.workflows?.total || 0
    bucket.total += day.tokens?.total || 0
    bucket.executions += day.executions
  }
  return [...buckets.values()]
}
export function share (tokens, total) {
  return tokens == null || !total ? '—' : `${(tokens / total * 100).toFixed(1)}%`
}
