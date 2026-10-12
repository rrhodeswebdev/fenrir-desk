import { expect, test } from '@playwright/test'

// Set by global-setup.ts after it opened the launch link; the server edits a temp copy of rules/.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

test('an edit saves, survives a reload, and a bad value shows its server error under the input', async ({ page }) => {
  await page.goto('/rules')
  await page.getByRole('link', { name: 'Edit lucid-flex-50k' }).click()
  const save = page.getByRole('button', { name: 'Save' })
  const minDays = page.getByRole('group', { name: 'Payout' }).getByLabel('Min days')
  await expect(save).toBeDisabled()
  await minDays.fill('6')
  await save.click()
  await expect(page.getByRole('status')).toHaveText('Saved.')
  await page.reload()
  await expect(minDays).toHaveValue('6')

  const drawdown = page.getByRole('group', { name: 'Funded' }).getByLabel('Max drawdown', { exact: true })
  await drawdown.fill('-5')
  await save.click()
  await expect(drawdown).toHaveAttribute('aria-invalid', 'true')
  await expect(drawdown).toHaveAccessibleDescription('Input should be greater than 0')
  await expect(page.getByRole('alert')).toContainText('funded.max_drawdown.amount: Input should be greater than 0')
})
