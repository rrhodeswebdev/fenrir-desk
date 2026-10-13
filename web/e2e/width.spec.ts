import { type Page, expect, test } from '@playwright/test'
import type { Period, Report } from '../src/backtests/Report'
import { account, deskJournal, status } from '../src/live/fixtures'

// Set by global-setup.ts after it opened the launch link.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

const MISSING = 'f'.repeat(64) // a report key the server has no file for: the 404 line
const KEY = 'e'.repeat(64)

const period = (start: string, end: string): Period => ({
  start,
  end,
  attempts: 1700,
  pass_rate: 0.31,
  days_to_pass: { median: 9, histogram: [[1, 4], [3, 11]] },
  failure_reasons: { flat_by: 3, max_drawdown: 900, no_result: 20 },
  payout_rate: 0.62,
  median_days_to_payout: 14,
  median_first_payout: 1250,
  p_pass_and_payout: 0.19,
  expected_gross_per_attempt: 87.5,
  expected_net_per_attempt: -42.5,
  microscalp_flags: 4,
  sample_equity: [{ start: '2023-05-01', outcome: 'payout', points: [['2023-05-01T20:59:00Z', 0], ['2023-05-02T20:59:00Z', 1250]] }],
})
const report: Report = {
  key: KEY,
  created_at: '2026-10-10T14:00:00Z',
  strategy: 'e2e_bracket',
  strategy_hash: 'a'.repeat(64),
  params: { or_minutes: 15, side: 'long' },
  account: 'lucid-flex-50k',
  rules_hash: 'b'.repeat(64),
  contract: 'MNQ',
  data_version: 'c'.repeat(64),
  holdout_months: 12,
  dll_on: true,
  periods: { tuning: period('2019-01-02', '2025-10-09'), holdout: period('2025-10-10', '2026-10-09') },
  holdout_gap: false,
  note: 'Attempts start on every trading day and overlap, so these rates are estimates, not exact probabilities.',
}

/** Every page, and what shows once its data is in (so the width is measured with the content on screen). */
const routes: [string, (page: Page) => Promise<void>][] = [
  ['/', (page) => expect(page.getByRole('region', { name: 'Latest backtest' })).toBeVisible()],
  ['/rules', (page) => expect(page.getByRole('link', { name: 'lucid-flex-50k' }).first()).toBeVisible()],
  ['/rules?account=lucid-flex-50k', (page) => expect(page.getByRole('button', { name: 'Save' })).toBeVisible()],
  ['/data', (page) => expect(page.getByRole('heading', { name: 'Coverage · 1-minute bars' })).toBeVisible()],
  ['/backtests', (page) => expect(page.getByRole('tabpanel')).toBeVisible()],
  [`/backtests/${MISSING}`, (page) => expect(page.getByRole('alert')).toContainText('404')],
  [`/backtests/${KEY}`, (page) => expect(page.locator('canvas').first()).toBeVisible()],
  ['/strategies', (page) => expect(page.getByRole('cell', { name: 'e2e_bracket.py' })).toBeVisible()],
  ['/live', (page) => expect(page.getByText('Book · LFE 0911')).toBeVisible()],
  ['/live/journal', (page) => expect(page.getByRole('main').getByRole('listitem').first()).toBeVisible()],
  ['/settings', (page) => expect(page.getByRole('button', { name: 'Save' })).toHaveCount(2)],
  ['/no-such-page', (page) => expect(page.getByRole('heading', { level: 1 })).toHaveText('Not found')],
]

// 340 px pins Settings' minmax(0, 1fr) page column: without it the accounts table widens the page below about 353 px.
for (const width of [340, 360, 390, 800, 1024, 1280]) {
  test(`no page scrolls sideways at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    // the report is a fixture (no e2e backtest needs to have run); the live status has an alarming account
    await page.route(`**/api/backtests/results/${KEY}`, (route) => route.fulfill({ json: report }))
    const live = status({ accounts: [account(), account({ account: 'LFE 0911', mode: 'sim', halted: true, halt_kind: 'reconcile', halt_reason: 'NT long 1, runner flat' })] })
    await page.route('**/api/live/status', (route) => route.fulfill({ json: live }))
    const rows = Object.values(deskJournal()).flat()
    await page.route('**/api/live/journal?*', (route) => route.fulfill({ json: rows }))
    for (const [path, ready] of routes) {
      await page.goto(path)
      await ready(page)
      await expect(page.getByText('Loading…')).toHaveCount(0)
      await page.evaluate(() => document.fonts.ready) // measure in the web fonts, not the fallback
      const scroll = await page.evaluate(() => document.documentElement.scrollWidth)
      expect.soft(scroll, `${path} at ${width} px`).toBeLessThanOrEqual(width)
    }
  })
}
