import { expect, test } from '@playwright/test'
import { execFile } from 'node:child_process'
import path from 'node:path'
import { promisify } from 'node:util'

// Set by global-setup.ts, which also wrote the fixture bars (tests/fixtures/e2e/make_bars.py) and strategy
// (tests/fixtures/e2e/e2e_bracket.py) into its temp dirs.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

// an account the rules spec never edits, so the two never share a file
const ACCOUNT = 'lucid-pro-25k'

test('a run from the UI goes to done over the socket, shows in compare, and opens its report', async ({ page }) => {
  test.setTimeout(180_000) // the backtest child is a real process: slow on a loaded machine
  await page.goto('/backtests')
  await page.getByRole('group', { name: 'Strategies' }).getByRole('checkbox', { name: /\/e2e_bracket\.py$/ }).check()
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
  await expect(compare).toContainText(ACCOUNT, { timeout: 30_000 })
  const result = compare.getByRole('row').filter({ has: page.getByRole('cell', { name: 'e2e_bracket', exact: true }) })
  await expect(result).toContainText('NQ')

  await row.getByRole('link', { name: 'Report' }).click()
  await expect(page).toHaveURL(/\/backtests\/[0-9a-f]{64}$/)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Report')
  // FENRIR_HOLDOUT_MONTHS=12 splits the fixture's two June weeks a year apart
  await expect(page.getByRole('columnheader', { name: 'Tuning · 2024-06-03 → 2024-06-06' })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('columnheader', { name: 'Hold-out · 2024-06-07 → 2025-06-06' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Equity samples · end-of-day P&L' })).toHaveCount(2)
  await expect(page.getByText('No samples.')).toHaveCount(0) // each period has a passed attempt
  await expect(page.locator('canvas').first()).toBeVisible()
})

// After the UI run above (tests in a file run in order), so no other spec writes a report meanwhile.
test('the Workbench pane shows a report the CLI writes within 5 s', async ({ page }) => {
  test.setTimeout(180_000)
  await page.goto('/')
  const pane = page.getByRole('region', { name: 'Latest backtest' })
  await expect(pane).toContainText(ACCOUNT)
  // the server's temp dirs (global-setup.ts); `fenrir backtest` is tuning only, so a new key beside the UI run's
  const tmp = path.dirname(process.env.FENRIR_E2E_ENV_FILE!)
  const { stdout } = await promisify(execFile)(
    'uv',
    ['run', 'fenrir', 'backtest', path.join(tmp, 'strategies', 'e2e_bracket.py'), '--account', ACCOUNT, '--contract', 'NQ', '--json'],
    {
      cwd: path.resolve(import.meta.dirname, '../..'),
      env: {
        ...process.env,
        FENRIR_DATA_DIR: path.join(tmp, 'data'),
        FENRIR_RULES_DIR: path.join(tmp, 'rules'),
        FENRIR_STRATEGIES_DIR: path.join(tmp, 'strategies'),
        FENRIR_PRIVATE_DIR: path.join(tmp, 'private'),
        FENRIR_HOLDOUT_MONTHS: '12',
        FENRIR_ENV_FILE: process.env.FENRIR_E2E_ENV_FILE,
      },
    },
  )
  const [{ key }] = JSON.parse(stdout) as [{ key: string }]
  await expect(pane.getByRole('link', { name: 'Full report' })).toHaveAttribute('href', `/backtests/${key}`, { timeout: 5_000 })
  await expect(pane.getByRole('row', { name: /^P\(pass\+payout\)/ })).toContainText('—') // tuning only: no hold-out
})
