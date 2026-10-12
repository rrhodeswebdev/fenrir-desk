import { expect, test } from '@playwright/test'

// Set by global-setup.ts, which also wrote the fixture bars (tests/fixtures/e2e/make_bars.py) into the temp data dir.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

// an account the rules spec never edits, so the two never share a file
const ACCOUNT = 'lucid-pro-25k'

test('a run from the UI goes to done over the socket, shows in compare, and opens its report', async ({ page }) => {
  test.setTimeout(180_000) // the backtest child is a real process: slow on a loaded machine
  await page.goto('/backtests')
  await page.getByRole('group', { name: 'Strategies' }).getByRole('checkbox', { name: /\/orb\.py$/ }).check()
  await page.getByRole('group', { name: 'Accounts' }).getByRole('checkbox', { name: ACCOUNT, exact: true }).check()
  await page.getByRole('group', { name: 'Contracts' }).getByRole('checkbox', { name: 'NQ', exact: true }).check()
  await page.getByRole('button', { name: 'Run' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Queued job' })).toBeVisible()

  // The job list never refetches: every status after the first GET arrives over /ws/jobs.
  const row = page.getByRole('region', { name: 'Jobs · latest 5' }).getByRole('row').filter({ hasText: ACCOUNT })
  await expect(row).toHaveCount(1)
  await expect(row.getByRole('cell').nth(3)).toHaveText(/^(pending|running)$/)
  await expect(row.getByRole('cell').nth(3)).toHaveText('done', { timeout: 150_000 })

  // the socket's item-finished invalidation refreshes the compare table in place
  const compare = page.getByRole('tabpanel')
  await expect(compare).toContainText(ACCOUNT)
  const result = compare.getByRole('row').filter({ has: page.getByRole('cell', { name: 'orb', exact: true }) })
  await expect(result).toContainText('NQ')

  await row.getByRole('link', { name: 'Report' }).click()
  await expect(page).toHaveURL(/\/backtests\/[0-9a-f]{64}$/)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Report')
  // FENRIR_HOLDOUT_MONTHS=12 splits the fixture's two June weeks a year apart
  await expect(page.getByRole('columnheader', { name: 'Tuning · 2024-06-03 → 2024-06-06' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: 'Hold-out · 2024-06-07 → 2025-06-06' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Equity samples · end-of-day P&L' })).toHaveCount(2)
  await expect(page.locator('canvas').first()).toBeVisible()
})
