import { test, expect } from './account-fixture.js'
import { mockDesign } from './design-fixture.js'

const summary = '**Unread messages**\n\n' + Array.from({ length: 30 }, (_, i) => `- Message ${i + 1}`).join('\n') + '\n- [Last message](https://example.com/inbox)'

for (const width of [1440, 360]) {
  test(`formats execution output and expands only when needed at ${width}px`, async ({ page, context }) => {
    await page.setViewportSize({ width, height: 900 })
    const state = await mockDesign(context)
    const run = { workflow_id: 'digest-1', workflow: 'digest', status: 'completed', trigger: 'manual', created_at: 1700000000, result: { summary, count: 31 } }
    state.workflow.runs = [run]
    await context.route('**/api/runs/digest-1', route => route.fulfill({ json: { run, temporal: { status: 'COMPLETED' } } }))
    await page.goto('/workflows/digest')
    const output = page.locator('.execution-output')
    const more = output.getByRole('button', { name: 'Show more' })
    const end = output.getByRole('link', { name: 'Last message' })
    await expect(output.locator('strong')).toHaveText('Unread messages')
    await expect(output.locator('li')).toHaveCount(31)
    await expect(more).toHaveAttribute('aria-expanded', 'false')
    await expect(end).not.toBeInViewport()
    await page.screenshot({ path: test.info().outputPath('latest-execution.png') })

    await more.press('Enter')
    const less = output.getByRole('button', { name: 'Show less' })
    await expect(less).toHaveAttribute('aria-expanded', 'true')
    await end.scrollIntoViewIfNeeded()
    await expect(end).toBeInViewport()
    await expect(end).toHaveAttribute('href', 'https://example.com/inbox')
    await less.click()
    await expect(end).not.toBeInViewport()
    await end.focus()
    await expect(less).toBeVisible()
    await expect(end).toBeInViewport()

    await page.getByRole('link', { name: 'View result' }).click()
    await expect(output.locator('strong')).toHaveText('Unread messages')
    await expect(more).toHaveAttribute('aria-expanded', 'false')
    const raw = page.getByText('Raw output', { exact: true })
    await raw.click()
    await expect(output.locator('pre')).toContainText('"count": 31')

    run.result = '**Short result**'
    await page.reload()
    await expect(output.locator('strong')).toHaveText('Short result')
    await expect(more).toHaveCount(0)
    await expect(raw).toHaveCount(0)
  })
}
