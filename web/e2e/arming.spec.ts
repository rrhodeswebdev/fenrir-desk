import { type Page, expect, test } from '@playwright/test'

// Mocks the arming API with page.route: pytest covers the server side of SC4 (tests/server/test_arming_routes.py).
test.use({ storageState: process.env.FENRIR_E2E_STATE })

const STRATEGY = '/repo/strategies/orb.py'
const strategies = [
  {
    name: 'orb',
    path: STRATEGY,
    hash: 'a'.repeat(64),
    market: 'NQ',
    params: { or_minutes: 15 },
    error: null,
    reports: [{ key: 'k1', account: 'lucid-flex-50k', contract: 'MNQ', created_at: '2026-10-12T10:00:00Z', periods: { tuning: { p_pass_and_payout: 0.19 }, holdout: null } }],
  },
]
const accounts = [{ id: 'LFE0507', name: 'LFE0507', is_sim: false, balance: 50250, realized_pnl_today: 0, unrealized_pnl: 0 }]
const summary = {
  start: '2019-01-02',
  end: '2025-10-09',
  attempts: 1700,
  pass_rate: 0.31,
  p_pass_and_payout: 0.19,
  expected_gross_per_attempt: 187.5,
  expected_net_per_attempt: 90,
  holdout_gap: true,
  strategy_hash: 'a'.repeat(64),
  rules_hash: 'b'.repeat(64),
  dll_on: true,
  liquidation_level: 48000,
}

/** Mocks every arming call; returns the arm POST bodies as they arrive. */
async function mockApi(page: Page): Promise<Record<string, unknown>[]> {
  const arms: Record<string, unknown>[] = []
  let armed: unknown[] = []
  await page.route('**/api/strategies', (route) => route.fulfill({ json: strategies }))
  await page.route('**/api/arming', (route) => route.fulfill({ json: armed }))
  await page.route('**/api/arming/accounts', (route) => route.fulfill({ json: accounts }))
  await page.route('**/api/arming/arm', (route) => {
    const body = route.request().postDataJSON()
    arms.push(body)
    if (!body.confirm) return route.fulfill({ status: 409, json: { error: 'needs_confirmation', summary } })
    armed = [{ ...body, strategy_hash: summary.strategy_hash, armed_at: '2026-10-13T14:05:09+00:00', dll_on: true, rules_hash: summary.rules_hash, instruments_hash: 'c'.repeat(64) }]
    return route.fulfill({ json: armed[0] })
  })
  return arms
}

async function choose(page: Page, label: string, option: string) {
  await page.getByLabel(label, { exact: true }).click()
  await page.getByRole('option', { name: option, exact: true }).click()
}

/** Fills the form for a live arm and presses Arm live: the first POST. */
async function armLive(page: Page) {
  await page.goto('/strategies')
  await choose(page, 'Strategy file', 'orb.py')
  await choose(page, 'Backtest', 'lucid-flex-50k · MNQ')
  await choose(page, 'NinjaTrader account', 'LFE0507 · live')
  await choose(page, 'Mode', 'Live')
  // prefilled with NinjaTrader's balance now; the user corrects the start
  await expect(page.getByLabel('Peak balance')).toHaveValue('50250')
  await page.getByLabel('Start balance').fill('50000')
  await page.getByRole('button', { name: 'Arm live' }).click()
}

test('a live arm shows the backtest summary before the second POST', async ({ page }) => {
  const arms = await mockApi(page)
  await armLive(page)
  const dialog = page.getByRole('alertdialog', { name: 'Arm live' })
  await expect(dialog).toBeVisible()
  expect(arms).toHaveLength(1)
  expect(arms[0]).toEqual({
    account: 'LFE0507',
    strategy_path: STRATEGY,
    rules_account: 'lucid-flex-50k',
    contract: 'MNQ',
    mode: 'live',
    phase: 'evaluation',
    confirm: false,
    start_balance: 50000,
    peak_balance: 50250,
  })
  for (const text of ['2019-01-02 to 2025-10-09', '1,700', '31.0%', '19.0%', '+$90.00', '$48,000.00', 'bbbbbbbbbbbb'])
    await expect(dialog).toContainText(text)
  await expect(dialog.getByText('Daily loss limit').locator('xpath=..')).toContainText('On')
  await expect(dialog.getByText(/^Yes\. Hold-out P\(pass and payout\) is under half/)).toHaveClass(/text-ember/)

  await dialog.getByRole('button', { name: 'Arm live' }).click()
  await expect(dialog).toBeHidden()
  expect(arms).toHaveLength(2)
  expect(arms[1]).toEqual({ ...arms[0], confirm: true, strategy_hash: summary.strategy_hash, rules_hash: summary.rules_hash, dll_on: true })
  await expect(page.getByRole('status')).toHaveText('Armed LFE0507: live.')
  await expect(page.getByRole('region', { name: 'Armed accounts' }).getByRole('row', { name: /LFE0507/ })).toContainText('Live')
})

test('Cancel sends nothing', async ({ page }) => {
  const arms = await mockApi(page)
  await armLive(page)
  const dialog = page.getByRole('alertdialog', { name: 'Arm live' })
  await expect(dialog).toBeVisible()
  // the safe choice has focus when the dialog opens
  await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toBeHidden()
  await page.waitForTimeout(500) // a stray confirm POST would land by now
  expect(arms).toHaveLength(1)
  await expect(page.getByRole('status')).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Armed accounts' })).toContainText('Nothing armed.')
})

test('Disarm asks first, then posts the account', async ({ page }) => {
  const disarms: unknown[] = []
  await mockApi(page)
  let armed = [{ account: 'LDA0042', strategy_path: STRATEGY, strategy_hash: 'a'.repeat(64), rules_account: 'lucid-flex-50k', contract: 'MNQ', mode: 'dry', phase: 'evaluation', armed_at: '2026-10-13T13:30:00+00:00', dll_on: false, rules_hash: 'b'.repeat(64), instruments_hash: 'c'.repeat(64), start_balance: null, peak_balance: null }]
  await page.route('**/api/arming', (route) => route.fulfill({ json: armed }))
  await page.route('**/api/arming/disarm', (route) => {
    disarms.push(route.request().postDataJSON())
    armed = []
    return route.fulfill({ json: { ok: true } })
  })
  await page.goto('/strategies')
  const region = page.getByRole('region', { name: 'Armed accounts' })
  await region.getByRole('button', { name: 'Disarm LDA0042' }).click()
  const dialog = page.getByRole('alertdialog', { name: 'Disarm LDA0042' })
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toBeHidden()
  expect(disarms).toEqual([])
  await region.getByRole('button', { name: 'Disarm LDA0042' }).click()
  await dialog.getByRole('button', { name: 'Disarm', exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(disarms).toEqual([{ account: 'LDA0042' }])
  await expect(region).toContainText('Nothing armed.')
})
