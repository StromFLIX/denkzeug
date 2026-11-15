import { createRouter, createWebHistory } from 'vue-router'
import { account } from './auth-session.js'
import { canOpenRoute } from './account.js'

const routes = [
  // Keep /chats as the mobile list/back destination; launch into the composer.
  { path: '/', redirect: { path: '/chats', query: { welcome: '1' } } },
  { path: '/chats/:id?', name: 'chats', component: () => import('./views/ChatPane.vue') },
  { path: '/workflows/:name?', name: 'workflows', component: () => import('./views/WorkflowPane.vue') },
  { path: '/workflows/:name/runs/:id', name: 'workflow-run', meta: { section: 'workflows' }, component: () => import('./views/RunPane.vue') },
  { path: '/runs', redirect: '/workflows?view=runs' },
  { path: '/runs/:id', name: 'runs', meta: { section: 'workflows' }, component: () => import('./views/RunPane.vue') },
  { path: '/apps/:id?', name: 'apps', component: () => import('./views/MiniAppPane.vue') },
  { path: '/settings/:tab?', name: 'settings', component: () => import('./views/SettingsPane.vue') },
  { path: '/:catchAll(.*)', redirect: '/chats' }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach(to => {
  if (account.user && !canOpenRoute(to)) return '/chats'
})

// The list a view sits on top of, and so the place its back button belongs.
const UNDER = { chats: '/chats', workflows: '/workflows', runs: '/workflows?view=runs', 'workflow-run': '/workflows?view=runs', apps: '/apps', settings: '/chats' }

/**
 * Opening straight onto a detail view (deep link, reload, Android cold start) leaves
 * nothing underneath it, so a back gesture would leave the app. Put its list under it
 * before the first render.
 */
export async function seedHistory () {
  // Resolve redirects first: / launches into the welcome composer, which is
  // also a full-screen detail on mobile and needs the chat list underneath it.
  await router.isReady()
  const entry = router.currentRoute.value
  const list = UNDER[entry.name]
  const welcome = (entry.name === 'chats' && entry.query.welcome === '1') || (entry.name === 'workflows' && entry.query.view === 'runs')
  // Vue Router persists its back entry across reloads. Keep that real origin
  // (including agent filters and settings callers) instead of inserting a new
  // list/detail pair every time the page or native webview is restored.
  if (window.history.state?.back || !list || (entry.path === list && !welcome)) return
  const parent = router.resolve(list)
  const query = ['chats', 'apps'].includes(entry.name) ? { ...parent.query, ...entry.query } : { ...parent.query }
  delete query.welcome
  await router.replace({ path: parent.path, query })
  await router.push(entry.fullPath)
}

/** Chat links retain the agent grouping, including on mobile back navigation. */
export function chatLocation (id = '', agent = router.currentRoute.value.query.agent) {
  return { path: id ? `/chats/${id}` : '/chats', query: typeof agent === 'string' && agent ? { agent } : {} }
}

/** Back out of a detail view without stacking history when that is where we came from. */
export function backTo (path) {
  const target = router.resolve(path).fullPath
  if (window.history.state?.back === target) router.back()
  else router.push(path)
}

export default router
