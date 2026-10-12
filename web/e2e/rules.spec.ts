import { expect, test } from '@playwright/test'
import { createHash } from 'node:crypto'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

// Set by global-setup.ts after it opened the launch link; the server edits a temp copy of rules/.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

const repoRules = path.resolve(import.meta.dirname, '../../rules')
const rulesHash = () => {
  const hash = createHash('sha256')
  for (const f of readdirSync(repoRules, { recursive: true, encoding: 'utf8' }).sort()) {
    const file = path.join(repoRules, f)
    if (statSync(file).isFile()) hash.update(f).update(readFileSync(file))
  }
  return hash.digest('hex')
}
const before = rulesHash()
test.afterAll(() => expect(rulesHash(), 'the repo rules/ changed').toBe(before))

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

// flex-25k, not flex-50k, so the two tests never share a file
test('scaling tiers: errors land under their input or at the list, and a tier adds and removes', async ({ page }) => {
  test.setTimeout(60_000) // five reloads and saves: slow on a loaded machine
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

  // removing a tier clears the last save's errors, whose indexes no longer match
  const tier1Minis = scaling.getByLabel('Tier 1 max minis')
  await tier1Minis.fill('0')
  await save.click()
  await expect(tier1Minis).toHaveAttribute('aria-invalid', 'true')
  await scaling.getByRole('button', { name: 'Remove tier 1' }).click()
  await expect(tier1Minis).toHaveValue('2')
  await expect(tier1Minis).toHaveAttribute('aria-invalid', 'false')
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.reload() // drop the unsaved removal

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
  // the top-tier check has loc [] and shows in the Scaling section too
  await save.click()
  await expect(scaling).toContainText('funded.scaling top tier allows 3 minis, contracts.max_minis is 2')
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

// a failing query retries 3 times (about 7s) before the page shows its error
test('a failing API call shows its status and body text', async ({ page }) => {
  await page.goto('/rules?account=no-such')
  await expect(page.getByRole('region', { name: 'Edit no-such' })).toContainText('404: unknown account no-such', { timeout: 15_000 })
})

test('with the server unreachable the page shows a readable error', async ({ page }) => {
  await page.route('**/api/**', (route) => route.abort('connectionrefused'))
  await page.goto('/rules')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Rules')
  await expect(page.getByText('Failed to fetch')).toBeVisible({ timeout: 15_000 })
})
