import { expect } from '@playwright/test'

export const workspaceSearch = page => page.getByRole('dialog', { name: 'Search workspace', exact: true })
export const workspaceQuery = page => workspaceSearch(page).getByRole('combobox', { name: 'Search workspace' })
export async function searchWorkspace (page, scope, query = '') {
  const scopedTrigger = page.getByRole('button', { name: `Search ${scope}`, exact: true })
  const globalTrigger = page.getByRole('button', { name: 'Search workspace', exact: true })
  await expect(scopedTrigger.or(globalTrigger).filter({ visible: true }).first()).toBeVisible()
  await (await scopedTrigger.isVisible() ? scopedTrigger : globalTrigger).click()
  const field = workspaceQuery(page)
  await expect(field).toBeFocused()
  const scopeLabel = { chats: 'Chats', workflows: 'Workflows', settings: 'Settings', runs: 'Runs' }[scope]
  if (scopeLabel) {
    const filter = workspaceSearch(page).getByRole('group', { name: 'Search in' }).getByRole('button', { name: scopeLabel, exact: true })
    if (await filter.getAttribute('aria-pressed') !== 'true') await filter.click()
  }
  await field.fill(query)
  return field
}

// Use the settings UI in a second tab so list/draft state is not reset by the test.
export async function setChatListPreferences (page, { group, minutes } = {}) {
  const settings = await page.context().newPage()
  try {
    await settings.goto('/settings/workspace')
    if (group !== undefined) await settings.getByLabel('Group chats by', { exact: true }).selectOption(group)
    if (minutes !== undefined) await settings.getByLabel('Activity window', { exact: true }).selectOption(String(minutes))
  } finally { await settings.close() }
}

export async function openChatConfiguration (page) {
  const panel = page.getByRole('region', { name: 'Chat preferences' })
  if (!await panel.isVisible()) await page.getByRole('button', { name: 'Chat configuration', exact: true }).click()
  await expect(panel).toBeVisible()
}

export async function closeChatConfiguration (page) {
  const panel = page.getByRole('region', { name: 'Chat preferences' })
  if (await panel.count()) await panel.getByRole('button', { name: 'Close chat preferences', exact: true }).click()
  await expect(panel).toBeHidden()
}
