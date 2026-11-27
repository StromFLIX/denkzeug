import { test, expect } from './account-fixture.js'
import { mockAgents } from './agent-fixture.js'

for (const width of [1440, 320]) {
  test(`nightly dreaming can be enabled, reviewed, edited and disabled at ${width}px`, async ({ page, context }) => {
    const state = await mockAgents(context)
    const agent = state.addAgent('Research')
    let dream = { schedule: null, dream: '', revision: 0, max_chars: 6000 }
    const updates = []
    await context.route(`**/api/agents/${agent.id}/dream`, route => {
      if (route.request().method() === 'GET') return route.fulfill({ json: dream })
      const update = route.request().postDataJSON()
      updates.push(update)
      if (update.revision !== dream.revision) return route.fulfill({ status: 409, json: { detail: 'This dream changed while you were editing. Reload it before saving.' } })
      dream = { ...dream, ...update, revision: dream.revision + 1 }
      return route.fulfill({ json: dream })
    })
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/settings/agents')
    await page.getByRole('button', { name: 'Edit Research', exact: true }).click()
    await page.getByText('Dreaming', { exact: true }).click()
    const toggle = page.getByRole('checkbox', { name: 'Reflect each night' })
    const save = page.getByRole('button', { name: 'Save dreaming', exact: true })
    const latest = page.getByRole('textbox', { name: 'Latest dream (Dreams.md)', exact: true })
    await expect(toggle).not.toBeChecked()
    await expect(latest).toHaveValue('')
    await toggle.check()
    await expect(save).toBeDisabled()
    await page.getByLabel('Nightly time').fill('23:30')
    await page.getByLabel('Timezone', { exact: true }).fill('Europe/Berlin')
    await save.click()
    await expect(page.getByText('Dreaming saved.', { exact: true })).toBeVisible()
    expect(updates).toEqual([{ revision: 0, schedule: { at: '23:30', timezone: 'Europe/Berlin' } }])
    expect(state.agentWrites).toHaveLength(0)
    expect(await page.locator('.settings__body').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)

    dream = { ...dream, revision: 2, dream: 'Use checklists. Verify the outcome.', last_status: 'updated', last_attempt_at: 1780000000 }
    const disclosure = page.locator('.dream summary')
    await disclosure.click()
    await disclosure.click()
    await expect(latest).toHaveValue('Use checklists. Verify the outcome.')
    await expect(page.getByText(/Last generated/)).toBeVisible()
    if (process.env.DREAM_SCREENSHOT_DIR) {
      await page.setViewportSize({ width, height: 1200 })
      await disclosure.scrollIntoViewIfNeeded()
      await page.locator('.dream').screenshot({ path: `${process.env.DREAM_SCREENSHOT_DIR}/dreaming-${width}.png` })
      await page.setViewportSize({ width, height: 900 })
    }
    await latest.fill('My unsaved revision')
    dream = { ...dream, revision: 3, dream: 'Verify the outcome before reporting.' }
    await save.click()
    await expect(page.getByRole('alert')).toContainText('This dream changed')
    await expect(latest).toHaveValue('My unsaved revision')
    await page.getByRole('button', { name: 'Reload saved dream' }).click()
    await expect(latest).toHaveValue('Verify the outcome before reporting.')
    await latest.fill('Keep checklists short.')
    await toggle.uncheck()
    await save.click()
    await expect(page.getByText('Dreaming saved.', { exact: true })).toBeVisible()
    expect(dream).toMatchObject({ schedule: null, dream: 'Keep checklists short.' })
    await page.getByRole('button', { name: 'Close agent editor' }).click()
    await page.getByRole('button', { name: 'Duplicate Research' }).click()
    await expect(page.getByText('Dreaming', { exact: true })).toHaveCount(0)
  })
}
