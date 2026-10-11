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

// flex-25k, not flex-50k: the test above edits that file on another worker
test('scaling tiers: errors land under their input or at the list, and a tier adds and removes', async ({ page }) => {
  await page.goto('/rules?account=lucid-flex-25k')
  const save = page.getByRole('button', { name: 'Save' })
  const scaling = page.getByRole('group', { name: 'Scaling' })
  const maxMinis = page.getByRole('group', { name: 'Contracts' }).getByLabel('Max minis')

  // a tier-order error comes back at ["funded","scaling"] and shows in the Scaling section
  await scaling.getByLabel('Tier 2 from profit').fill('0')
  await save.click()
  await expect(page.getByRole('alert')).toContainText('funded.scaling: Value error, tiers must start at from_profit 0 and strictly increase')
  await expect(scaling).toContainText('tiers must start at from_profit 0 and strictly increase')
  await scaling.getByLabel('Tier 2 from profit').fill('1000')

  // an error inside one tier lands under that tier's input
  const tier2Minis = scaling.getByLabel('Tier 2 max minis')
  await tier2Minis.fill('0')
  await save.click()
  await expect(tier2Minis).toHaveAttribute('aria-invalid', 'true')
  await expect(tier2Minis).toHaveAccessibleDescription('Input should be greater than 0')
  await tier2Minis.fill('2')

  await scaling.getByRole('button', { name: 'Add tier' }).click()
  await scaling.getByLabel('Tier 3 from profit').fill('2000')
  await scaling.getByLabel('Tier 3 max minis').fill('3')
  await maxMinis.fill('3')
  await save.click()
  await expect(page.getByRole('status')).toHaveText('Saved.')
  await page.reload()
  await expect(scaling.getByLabel('Tier 3 max minis')).toHaveValue('3')

  await scaling.getByRole('button', { name: 'Remove tier 3' }).click()
  await maxMinis.fill('2')
  await save.click()
  await expect(page.getByRole('status')).toHaveText('Saved.')
  await page.reload()
  await expect(scaling.getByLabel('Tier 2 max minis')).toHaveValue('2')
  await expect(scaling.getByLabel('Tier 3 max minis')).toHaveCount(0)
})
