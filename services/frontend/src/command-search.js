import { SETTINGS_SECTIONS, searchSettings } from './settings-registry.js'
import { fullTime } from './format.js'
import { runLocation } from './workflow-inputs.js'

const normalize = value => String(value ?? '').toLocaleLowerCase().trim()
const wordsIn = query => normalize(query).split(/\s+/).filter(Boolean)
const matchesWords = (text, words) => words.every(word => normalize(text).includes(word))
const resultGroup = (key, label, results, limit = 30) => ({ key, label, count: results.length, results: results.slice(0, limit) })

// Search the full chat list, independently of sidebar grouping/activity filters.
export function searchChats (chats, query) {
  const words = wordsIn(query)
  const matches = text => matchesWords(text, words)
  return chats.filter(chat => matches(`${chat.title || ''} ${chat.last_message?.preview || ''}`))
    .sort((a, b) => Number(matches(b.title)) - Number(matches(a.title)))
}

export function commandSearchGroups (chats, query, scope = 'all', remoteResults = null,
  { workflows = [], drafts = [], runs = [], packages = [], settingSections = SETTINGS_SECTIONS } = {}) {
  const text = query.trim()
  // null means no server result yet (or offline): retain the local fallback.
  const chatResults = remoteResults === null ? searchChats(chats, text) : remoteResults.map(result => ({
    id: result.chat_id, title: result.title, messageId: result.message_id,
    last_message: { preview: `${result.match === 'semantic' ? 'Related · ' : ''}${result.excerpt}` }
  }))
  const browseSettings = !text || /^(settings|preferences|einstellungen)$/i.test(text)
  const settingResults = browseSettings
    ? settingSections.map(section => ({ id: '', label: section.label, description: `${section.group} · ${section.scope}`, section }))
    : searchSettings(text, settingSections)
  const words = wordsIn(text)
  // The list and the detail route both prefer a draft over a deployed revision.
  const workflowsByName = new Map([...workflows, ...drafts.map(item => ({ ...item, draft: true }))].map(item => [item.name, item]))
  const workflowResults = [...workflowsByName.values()].filter(item => matchesWords(`${item.name} ${item.title || ''} ${item.description || ''}`, words))
  const groups = [
    resultGroup('chats', text ? 'Chats' : 'Recent chats', chatResults.map(chat => ({
      key: `chat-${chat.id}`, label: chat.title || 'Untitled chat',
      description: chat.last_message?.preview || 'Open conversation', icon: 'chat_bubble_outline',
      to: { path: `/chats/${encodeURIComponent(chat.id)}`, ...(chat.messageId ? { hash: `#message-${chat.messageId}` } : {}) }
    })), text ? 30 : 8),
    resultGroup('workflows', 'Workflows', workflowResults.map(item => ({
      key: `workflow-${item.name}`, label: item.title || item.name,
      description: [item.draft ? 'Draft' : '', item.description || item.name].filter(Boolean).join(' · '), icon: 'account_tree',
      to: { path: `/workflows/${encodeURIComponent(item.name)}` }
    })), text || scope === 'workflows' ? 30 : 6),
    resultGroup('runs', text ? 'Runs' : 'Recent runs', runs.map(item => ({
      key: `run-${item.workflow_id}`, label: workflowsByName.get(item.workflow)?.title || item.workflow || item.workflow_id,
      description: [item.status?.replaceAll('_', ' '), item.created_at && fullTime(item.created_at), item.workflow_id].filter(Boolean).join(' · '),
      icon: 'history', to: { path: item.workflow ? runLocation(item.workflow, item.workflow_id) : `/runs/${encodeURIComponent(item.workflow_id)}` }
    })), text || scope === 'runs' ? 30 : 6),
    resultGroup('settings', 'Settings', settingResults.map((item, index) => ({
      key: `setting-${item.section.key}-${item.id}-${index}`, label: item.label,
      description: browseSettings ? item.description : `${item.section.label} · ${item.description}`,
      icon: item.section.icon,
      to: { path: `/settings/${item.section.key}`, ...(item.id ? { hash: `#${item.id}` } : {}) }
    }))),
    resultGroup('packages', 'Pi packages on npm', packages.map(item => ({
      key: `package-${item.name}`, label: item.name,
      description: [item.version, item.description].filter(Boolean).join(' · '),
      icon: 'extension', to: { path: '/settings/packages', query: { source: `npm:${item.name}@${item.version}` } }
    })), 100)
  ].filter(group => (scope === group.key || (scope === 'all' && group.key !== 'packages')) && group.results.length)
  // Leave room for secondary matches. The Chats filter shows the deeper list.
  return scope === 'all' && text && groups.length > 1 ? groups.map(group => group.key === 'chats'
    ? { ...group, results: group.results.slice(0, 5) } : group) : groups
}
