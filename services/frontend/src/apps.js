/** Portable app source only. Importing never publishes or executes the package. */
export const APP_PACKAGE_VERSION = 1
export const MAX_APP_PACKAGE_BYTES = 2 * 1024 * 1024

export function appDraft (source = {}) {
  return {
    id: source.id || '', title: source.title || '', description: source.description || '',
    html: source.html || '', input_schema: JSON.stringify(source.input_schema ?? { type: 'object', properties: {} }, null, 2),
    sample_data: JSON.stringify(source.sample_data ?? {}, null, 2), change_note: ''
  }
}

export function parseAppObject (text, label) {
  let value
  try { value = JSON.parse(text) } catch { throw new Error(`${label} must be valid JSON.`) }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} must be a JSON object.`)
  return value
}

export function appPayload (draft, expectedRevision = 0) {
  const id = draft.id.trim(), title = draft.title.trim()
  if (!/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(id)) throw new Error('Use a slug starting with a letter, with lowercase letters, numbers and single hyphens.')
  if (!title) throw new Error('Enter an app title.')
  if (!draft.html.trim()) throw new Error('Add self-contained HTML source before publishing.')
  return {
    id, title, description: draft.description.trim(), html: draft.html,
    input_schema: parseAppObject(draft.input_schema, 'Input schema'),
    sample_data: parseAppObject(draft.sample_data, 'Sample data'),
    change_note: draft.change_note.trim(), expected_revision: expectedRevision
  }
}

export function exportAppPackage (source) {
  const payload = appPayload(appDraft(source))
  const { expected_revision, change_note, ...content } = payload
  return JSON.stringify({ version: APP_PACKAGE_VERSION, ...content }, null, 2)
}

export function importAppPackage (text) {
  if (new TextEncoder().encode(text).length > MAX_APP_PACKAGE_BYTES) throw new Error('App packages must be 2 MB or smaller.')
  const data = parseAppObject(text, 'App package')
  if (data.version !== undefined && data.version !== APP_PACKAGE_VERSION) throw new Error('Unsupported app package version.')
  if (typeof data.id !== 'string' || typeof data.title !== 'string' || typeof data.html !== 'string' || (data.description !== undefined && typeof data.description !== 'string')) throw new Error('App packages need a slug, title and HTML source as text.')
  if ('input_schema' in data) parseAppObject(JSON.stringify(data.input_schema), 'Input schema')
  if ('sample_data' in data) parseAppObject(JSON.stringify(data.sample_data), 'Sample data')
  const draft = appDraft(data)
  appPayload(draft)
  return draft
}
