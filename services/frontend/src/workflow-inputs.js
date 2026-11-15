// A lossless form adapter, not a replacement for server-side JSON Schema validation.
export const owns = (value, key) => Object.hasOwn(value || {}, key)
export const inputPointer = key => '/' + key.replaceAll('~', '~0').replaceAll('/', '~1')
export const inputKey = path => path?.startsWith('/') ? path.slice(1).split('/')[0].replaceAll('~1', '/').replaceAll('~0', '~') : path
export function choices (schema) {
  if (Array.isArray(schema.enum) && schema.enum.every(value => value == null || ['string', 'number', 'boolean'].includes(typeof value))) {
    const labels = schema.enum.map(value => String(value))
    return schema.enum.map((value, index) => ({ value, label: schema.x_ui?.enum_labels?.[index] ??
      (labels.filter(label => label === labels[index]).length > 1 ? `${String(value)} (${value === null ? 'null' : typeof value})` : String(value)) }))
  }
  if (Array.isArray(schema.oneOf) && schema.oneOf.every(item => owns(item, 'const') && (item.const == null || ['string', 'number', 'boolean'].includes(typeof item.const)))) {
    return schema.oneOf.map(item => ({ value: item.const, label: item.title || String(item.const) }))
  }
  if (owns(schema, 'const')) return [{ value: schema.const, label: String(schema.const) }]
  return null
}
export function baseType (schema) {
  return Array.isArray(schema.type) ? schema.type.filter(type => type !== 'null').join('|') : schema.type
}
export function fieldKind (schema) {
  if (['agent', 'chat'].includes(schema.x_resource?.kind)) return 'resource'
  if (choices(schema)) return 'choice'
  if (schema.anyOf || schema.oneOf || schema.allOf || schema.$ref) return 'json'
  const type = baseType(schema)
  return ['string', 'integer', 'number', 'boolean'].includes(type) ? type : 'json'
}
export const nullable = schema => schema.type === 'null' || (Array.isArray(schema.type) && schema.type.includes('null'))
export const cloneInputs = value => JSON.parse(JSON.stringify(value))
export function initialInputs (schema = {}, saved = {}) {
  const value = cloneInputs(saved)
  for (const [key, field] of Object.entries(schema.properties || {})) {
    if ((schema.required || []).includes(key) && !owns(value, key)) {
      if (owns(field, 'default')) Object.defineProperty(value, key, { value: cloneInputs(field.default), enumerable: true, writable: true, configurable: true })
      else if (owns(field, 'const')) Object.defineProperty(value, key, { value: cloneInputs(field.const), enumerable: true, writable: true, configurable: true })
    }
  }
  return value
}
export function inputErrors (schema, payload) {
  const errors = {}
  for (const [key, field] of Object.entries(schema.properties || {})) {
    const present = owns(payload, key), value = payload[key]
    if (!present || value === undefined) {
      if ((schema.required || []).includes(key)) errors[key] = 'Supply a value for this required input.'
      continue
    }
    const allowed = choices(field)
    if (allowed && !allowed.some(item => JSON.stringify(item.value) === JSON.stringify(value))) errors[key] = 'Choose an allowed value.'
    if (value === null) {
      if (!nullable(field) && !allowed?.some(item => item.value === null)) errors[key] = 'This input does not accept null.'
      continue
    }
    const type = baseType(field)
    if (['number', 'integer'].includes(type)) {
      if (typeof value !== 'number' || !Number.isFinite(value) || (type === 'integer' && !Number.isInteger(value))) errors[key] = `Enter a valid ${type}.`
      else if (field.minimum !== undefined && value < field.minimum) errors[key] = `Minimum: ${field.minimum}.`
      else if (field.maximum !== undefined && value > field.maximum) errors[key] = `Maximum: ${field.maximum}.`
    }
    if (type === 'boolean' && typeof value !== 'boolean') errors[key] = 'Choose Yes or No.'
    if (type === 'string') {
      if (typeof value !== 'string') errors[key] = 'Enter text.'
      else if (field.minLength !== undefined && value.length < field.minLength) errors[key] = `Use at least ${field.minLength} characters.`
      else if (field.maxLength !== undefined && value.length > field.maxLength) errors[key] = `Use at most ${field.maxLength} characters.`
    }
    if (type === 'array' && !Array.isArray(value)) errors[key] = 'Enter a JSON array.'
    if (type === 'object' && (typeof value !== 'object' || Array.isArray(value))) errors[key] = 'Enter a JSON object.'
  }
  return errors
}
export const runLocation = (name, id) => `/workflows/${encodeURIComponent(name)}/runs/${encodeURIComponent(id)}`
export const runListLocation = name => name ? `/workflows/${encodeURIComponent(name)}?tab=Runs` : '/workflows?view=runs'
