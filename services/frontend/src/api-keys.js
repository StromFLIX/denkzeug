export const tokenDate = value => value == null ? 'Never used' : new Date(value * 1000).toLocaleString()
export function tokenState (token, now = Date.now() / 1000) {
  if (token.revoked_at != null) return 'Revoked'
  return token.expires_at <= now ? 'Expired' : 'Active'
}

/** Never infer permissions from labels or send display names as resource IDs. */
export function tokenPayload (name, days, selected, catalog, now = Date.now() / 1000) {
  if (!name.trim()) throw new Error('Give this key a name so you can recognize it later.')
  if (!Number.isInteger(Number(days)) || Number(days) < 1 || Number(days) > catalog.policy.max_days) throw new Error(`Choose an expiry between 1 and ${catalog.policy.max_days} days.`)
  const grants = Object.entries(selected).map(([scope, choice]) => {
    const definition = catalog.scopes.find(item => item.scope === scope)
    if (!definition) throw new Error('Permissions changed. Refresh and choose them again.')
    if (!definition.resource_kind || choice.all) return { scope, resources: null }
    const resources = [...new Set(choice.resources)]
    const available = new Set(catalog.resources.filter(item => item.kind === definition.resource_kind).map(item => item.id))
    if (!resources.length || resources.some(id => !available.has(id))) throw new Error(`Choose at least one available resource for ${scope}.`)
    return { scope, resources }
  })
  if (!grants.length) throw new Error('Choose at least one permission.')
  return { name: name.trim(), expires_at: Math.floor(now) + Number(days) * 86400, grants }
}

/** Include quiet days so the chart does not imply continuous activity. UTC day buckets. */
export function dailyTokenUsage (days, now = Date.now() / 1000, count = 30) {
  const end = Math.floor(now / 86400) * 86400
  const byDay = new Map(days.map(item => [item.day, item]))
  return Array.from({ length: count }, (_, index) => {
    const day = end - (count - 1 - index) * 86400
    return byDay.get(day) || { day, requests: 0, failures: 0 }
  })
}
