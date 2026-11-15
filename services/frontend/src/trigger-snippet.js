/** Snippets accept only a dedicated workflow URL, never an account/session token. */
export const triggerLanguages = ['curl', 'python', 'node', 'GET']
export const shellQuote = value => `'${String(value).replaceAll("'", "'\\''")}'`

export function triggerPreviewUrl (origin, workflow) {
  return `${origin.replace(/\/+$/, '')}/api/triggers/${encodeURIComponent(workflow)}?token=YOUR_WORKFLOW_TOKEN`
}

export function triggerSnippet (language, url, inputs = {}) {
  const json = JSON.stringify(inputs)
  // A quoted JSON string is valid in both Python and JavaScript. Parse it rather
  // than interpreting JSON booleans/null as Python code or __proto__ as JS syntax.
  const literal = JSON.stringify(json)
  if (language === 'curl') return `curl -X POST ${shellQuote(url)} \\\n  -H 'Content-Type: application/json' \\\n  --data-raw ${shellQuote(json)}`
  if (language === 'python') return `import json\nimport httpx\n\nhttpx.post(\n    ${JSON.stringify(url)},\n    json=json.loads(${literal}),\n).raise_for_status()`
  if (language === 'node') return `const response = await fetch(${JSON.stringify(url)}, {\n  method: "POST",\n  headers: { "Content-Type": "application/json" },\n  body: JSON.stringify(JSON.parse(${literal}))\n})\nif (!response.ok) throw new Error(\`HTTP \${response.status}\`)`
  if (language === 'GET') {
    const link = new URL(url)
    link.searchParams.set('input', json)
    return link.href
  }
  throw new Error('Choose a supported integration format.')
}
