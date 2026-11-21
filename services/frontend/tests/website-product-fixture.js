import { mockAgents } from './agent-fixture.js'

// Fictional demonstration only. No account, customer data, model calls or generated
// files are involved: metadata illustrates the real app's published-output UI.
export const captureTime = new Date('2026-06-15T10:30:00Z')
export const chatPath = '/chats/alpha'
export const briefingTitle = 'Weekly briefing'
const briefing = { id: 'weekly-briefing', title: briefingTitle, app_id: 'weekly-briefing', revision: 1 }

// Styles inside the demo MCP App belong to its authored HTML, not to the host UI.
export function briefingHtml (script) {
  return `<!doctype html><html lang="en"><meta charset="utf-8"><title>${briefingTitle}</title>
<style>
  *{box-sizing:border-box}body{margin:0;padding:20px;font:15px/1.5 system-ui,sans-serif;color:#202744;background:#f2f4fc}
  header{display:flex;justify-content:space-between;align-items:start;gap:8px;flex-wrap:wrap}
  h2{margin:0;font-size:22px;letter-spacing:-.025em}p{margin:2px 0 12px;color:#495578}
  select{font:inherit;color:inherit;background:#fff;border:1px solid #aeb7d5;border-radius:6px;padding:6px}
  ul{list-style:none;padding:0;margin:0;border-top:1px solid #c8d0e8}
  li{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid #c8d0e8}
  li span{font-size:12px;color:#334887;background:#e1e7fb;border-radius:4px;padding:2px 7px}
</style>
<body><header><div><h2>Weekly briefing</h2><p>What changed and what needs a decision.</p></div>
<label>Filter <select><option value="">All updates</option><option>Decision</option><option>Shipped</option></select></label></header>
<ul aria-label="Briefing updates"></ul><script>${script.replace(/<\/script/gi, '<\\/script')}</script></body></html>`
}

export async function mockWebsiteProduct (context, app) {
  const state = await mockAgents(context)
  const now = captureTime.getTime() / 1000
  const research = state.addAgent('Research')
  state.addAgent('Builder')
  const writing = state.addAgent('Writing')
  Object.assign(state.data.chat, {
    title: 'Product brief & slides', agent_id: writing.id, agent_name: writing.name,
    agent_customized: false, answering: false, created_at: now - 600, updated_at: now,
    last_read_message_id: 'answer'
  })
  const output = {
    id: 'a'.repeat(32), revision: 1, title: 'Product brief & slides',
    summary: 'A focused brief, an editable deck and a PDF to share.',
    source_app: 'WordCraft · DeckCraft', turn_id: 'sample-turn', created_at: now,
    files: [
      { id: 'brief-source', name: 'product-brief.docx', size: 28672, mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', role: 'source', previewable: false },
      { id: 'deck-source', name: 'product-slides.pptx', size: 94208, mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', role: 'source', previewable: false },
      { id: 'share-pdf', name: 'brief-and-slides.pdf', size: 126976, mime_type: 'application/pdf', role: 'file', previewable: false }
    ]
  }
  const answer = '## Ready to refine\n\nThe brief covers the audience and first release. The six-slide deck is editable; open questions are marked.'
  state.data.messages = [
    { id: 'question', role: 'user', created_at: now - 120, meta: {}, content: 'Create a product brief and an editable slide deck from these notes. Calmer team workspace: conversations, research and files together.' },
    { id: 'answer', role: 'assistant', created_at: now - 30, content: answer, meta: { outputs: [output], steps: [
      { kind: 'tool', id: 'brief', name: 'wordcraft_create_document', ok: true, args: { title: 'Product brief' }, result: 'Sample brief prepared.' },
      { kind: 'tool', id: 'slides', name: 'deckcraft_create_presentation', ok: true, args: { title: 'Product slides' }, result: 'Sample deck prepared.' },
      { kind: 'tool', id: 'publish', name: 'publish_output', ok: true, args: { title: output.title }, result: 'Sample output metadata ready.' },
      { kind: 'text', text: answer }
    ] } }
  ]
  state.data.outputs = [output]
  state.data.active_turn = null
  for (const [index, title] of ['Reading list for the week', 'Plan the first release', 'A clearer onboarding flow', 'Notes for the next workshop', 'Research questions', 'Turn notes into next steps'].entries()) {
    const id = `sample-${index}`
    state.chats[id] = { chat: { ...state.data.chat, id, title, agent_id: index % 2 ? writing.id : research.id, agent_name: index % 2 ? 'Writing' : 'Research', created_at: now - 3600 * (index + 1), updated_at: now - 3600 * (index + 1), last_read_message_id: null }, messages: [], active_turn: null }
  }
  if (app) {
    state.chats.briefing = {
      chat: { ...state.data.chat, id: 'briefing', title: briefingTitle, updated_at: now - 1800, last_read_message_id: 'briefing-result' },
      active_turn: null,
      messages: [
        { id: 'briefing-request', role: 'user', created_at: now - 1860, content: 'What shipped this week, and what still needs a decision?', meta: {} },
        { id: 'briefing-result', role: 'assistant', created_at: now - 1800, content: 'The weekly briefing is ready. Filter for decisions to see what needs your input.', meta: { apps: [briefing], origin: { kind: 'workflow', workflow: 'digest', relation: 'result' } } }
      ]
    }
  }
  const run = { workflow_id: 'digest-record', workflow: 'digest', status: 'completed', input: { topic: 'Product updates' }, result: { summary: 'Weekly product notes prepared', items: 6 }, created_at: now - 900, trigger: 'manual' }
  Object.assign(state.workflow, { description: 'Collect product updates and prepare a focused morning summary.', runs: [run] })
  const statistics = {
    available: true, coverage: 'recorded', agent_calls: 3, model_requests: 7,
    tool_calls: 5, mcp_calls: 4, direct_mcp_calls: 2, usage_reports: 7,
    tokens: { input: 1000, output: 200, cacheRead: 30, cacheWrite: 4, total: 1234 }, usage_complete: true,
    agents: [{ agent_set: 'research', model: 'demo/research-model', calls: 2, completed: 2 }, { agent_set: 'default', model: 'demo/writer-model', calls: 1, completed: 1 }],
    tools: [{ name: 'github_search_repositories', source: 'workflow', calls: 2, completed: 2 }, { name: 'github_search_repositories', source: 'agent_mcp', calls: 2, completed: 2 }, { name: 'bash', source: 'local', calls: 1, completed: 1 }]
  }
  // Register last, before the reusable fixture fallback. An empty but valid draft
  // avoids a misleading 'Draft sync unavailable' notice in the composer.
  const requests = []
  await context.route('**/api/**', async route => {
    const { pathname } = new URL(route.request().url())
    const method = route.request().method()
    requests.push({ path: pathname, method })
    if (method !== 'GET' && method !== 'HEAD' && !pathname.endsWith('/read-state')) throw new Error(`Unexpected capture mutation: ${method} ${pathname}`)
    if (pathname.endsWith('/draft')) return route.fulfill({ json: { text: '', attachments: [], revision: 0 } })
    if (app && pathname === '/api/mcp-apps/runtime') return route.fulfill({ json: { sandbox_url: app.origin } })
    if (app && pathname === '/api/chats/briefing/apps/weekly-briefing') return route.fulfill({ json: {
      html: app.html, title: briefingTitle, input: {}, result: { content: [], structuredContent: { items: [
        { title: 'Pick the next onboarding experiment', group: 'Decision' },
        { title: 'Publish the revised onboarding guide', group: 'Shipped' },
        { title: 'Choose a date for the feedback session', group: 'Decision' }
      ] } }
    } })
    if (pathname === '/api/drafts') return route.fulfill({ json: { drafts: [] } })
    if (pathname === '/api/workflows') return route.fulfill({ json: { workflows: [state.workflow] } })
    if (pathname === '/api/runs') return route.fulfill({ json: { runs: [run] } })
    if (pathname === '/api/runs/digest-record') return route.fulfill({ json: { run, statistics, temporal: { status: 'COMPLETED', close_time: '2026-06-15T10:16:00Z' } } })
    if (pathname === '/api/runs/digest-record/graph') return route.fulfill({ json: { ...state.workflow.graph, mode: 'execution', status: 'completed' } })
    return route.fallback()
  })
  await context.addInitScript(() => {
    if (window !== window.top) return
    localStorage.setItem('nautionette.token', 'sample-capture-token-not-a-credential')
    localStorage.setItem('nautionette.preferences.v1', JSON.stringify({ theme: 'denkzeug', skin: '', interfaceSize: 125 }))
  })
  return { state, requests }
}
