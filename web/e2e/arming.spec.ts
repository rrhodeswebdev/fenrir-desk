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

type Summary = typeof summary
const armedRow = (body: Record<string, unknown>, s: Summary) => ({ ...body, strategy_hash: s.strategy_hash, armed_at: '2026-10-13T14:05:09+00:00', dll_on: s.dll_on, rules_hash: s.rules_hash, instruments_hash: 'c'.repeat(64) })

/** Mocks every arming call like the server: a live arm counts only when it confirms and echoes `current.summary`
 * (a test can swap it, as the terminal could); a second arm of an account is already_armed. Returns the arm POST
 * bodies as they arrive; `delayMs` holds each reply. */
async function mockApi(page: Page, current = { summary }, delayMs = 0): Promise<Record<string, unknown>[]> {
  const arms: Record<string, unknown>[] = []
  let armed: Record<string, unknown>[] = []
  await page.route('**/api/strategies', (route) => route.fulfill({ json: strategies }))
  await page.route('**/api/arming', (route) => route.fulfill({ json: armed }))
  await page.route('**/api/arming/accounts', (route) => route.fulfill({ json: accounts }))
  await page.route('**/api/arming/arm', async (route) => {
    const body = route.request().postDataJSON()
    arms.push(body)
    if (delayMs) await new Promise((done) => setTimeout(done, delayMs))
    const s = current.summary
    if (armed.some((a) => a.account === body.account)) return route.fulfill({ status: 409, json: { error: 'already_armed', summary: null } })
    const confirmed = body.confirm === true && body.strategy_hash === s.strategy_hash && body.rules_hash === s.rules_hash && body.dll_on === s.dll_on
    if (body.mode === 'live' && !confirmed) return route.fulfill({ status: 409, json: { error: 'needs_confirmation', summary: s } })
    armed = [...armed, armedRow(body, s)]
    return route.fulfill({ json: armed.at(-1) })
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
  // Escape closes it the same way
  await page.getByRole('button', { name: 'Arm live' }).click()
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await page.waitForTimeout(500) // a stray confirm POST would land by now
  expect(arms.map((a) => a.confirm)).toEqual([false, false])
  await expect(page.getByRole('status')).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Armed accounts' })).toContainText('Nothing armed.')
})

test('a stale confirm shows the new summary, sends nothing more on its own, and the next press echoes the new hash', async ({ page }) => {
  const current = { summary }
  const arms = await mockApi(page, current)
  await armLive(page)
  const dialog = page.getByRole('alertdialog', { name: 'Arm live' })
  await expect(dialog).toBeVisible()
  current.summary = { ...summary, strategy_hash: 'd'.repeat(64), p_pass_and_payout: 0.55 } // the terminal swapped the file
  await dialog.getByRole('button', { name: 'Arm live' }).click()
  await expect(dialog).toContainText('Changed since you looked')
  await expect(dialog).toContainText('dddddddddddd')
  await expect(dialog).toContainText('55.0%')
  // the keyboard stays in the dialog, on the safe choice
  await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await page.waitForTimeout(500)
  expect(arms).toHaveLength(2) // nothing re-sent on its own
  await dialog.getByRole('button', { name: 'Arm live' }).click()
  await expect(dialog).toBeHidden()
  expect(arms[2]).toEqual({ ...arms[0], confirm: true, strategy_hash: 'd'.repeat(64), rules_hash: summary.rules_hash, dll_on: true })
  await expect(page.getByRole('status')).toHaveText('Armed LFE0507: live.')
})

test('a double click on Arm live sends one confirm', async ({ page }) => {
  const arms = await mockApi(page, { summary }, 100)
  await armLive(page)
  const dialog = page.getByRole('alertdialog', { name: 'Arm live' })
  await dialog.getByRole('button', { name: 'Arm live' }).dblclick()
  await expect(dialog).toBeHidden()
  await page.waitForTimeout(300)
  expect(arms.map((a) => a.confirm)).toEqual([false, true])
  // the same account again: the server's already_armed shows in words under the form, no dialog
  await page.getByRole('button', { name: 'Arm live' }).click()
  await expect(page.getByRole('alert')).toHaveText('409: This account is already armed. Disarm it first.')
  await expect(dialog).toBeHidden()
})

test('Disarm asks first, then posts the account', async ({ page }) => {
  const disarms: unknown[] = []
  await mockApi(page)
  let armed = [{ account: 'LDA0042', strategy_path: STRATEGY, strategy_hash: 'a'.repeat(64), rules_account: 'lucid-flex-50k', contract: 'MNQ', mode: 'dry', phase: 'evaluation', armed_at: '2026-10-13T13:30:00+00:00', dll_on: false, rules_hash: 'b'.repeat(64), instruments_hash: 'c'.repeat(64), start_balance: null, peak_balance: null }]
  await page.route('**/api/arming', (route) => route.fulfill({ json: armed }))
  await page.route('**/api/arming/disarm', (route) => {
    disarms.push(route.request().postDataJSON())
    armed = []
    return route.fulfill({ json: { ok: true, error: null } })
  })
  await page.goto('/strategies')
  const region = page.getByRole('region', { name: 'Armed accounts' })
  await region.getByRole('button', { name: 'Disarm LDA0042' }).click()
  const dialog = page.getByRole('alertdialog', { name: 'Disarm LDA0042' })
  await expect(dialog).toContainText('A dry run sends nothing to NinjaTrader, so there is nothing to flatten.')
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toBeHidden()
  expect(disarms).toEqual([])
  await region.getByRole('button', { name: 'Disarm LDA0042' }).click()
  await dialog.getByRole('button', { name: 'Disarm', exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(disarms).toEqual([{ account: 'LDA0042' }])
  await expect(region).toContainText('Nothing armed.')
})

for (const mode of ['sim', 'live'] as const) {
  test(`a ${mode} disarm says the position is flattened first, and a failed flatten says it may still be open (#223, #227)`, async ({ page }) => {
    await mockApi(page)
    const armed = [{ account: 'Sim101', strategy_path: STRATEGY, strategy_hash: 'a'.repeat(64), rules_account: 'lucid-flex-50k', contract: 'MNQ', mode, phase: 'evaluation', armed_at: '2026-10-13T13:30:00+00:00', dll_on: false, rules_hash: 'b'.repeat(64), instruments_hash: 'c'.repeat(64), start_balance: 50000, peak_balance: 50500 }]
    const error = 'disarm failed: flatten failed: ConnectError: add-on unreachable; the position may still be open: check NinjaTrader; still armed, halted until disarmed'
    await page.route('**/api/arming', (route) => route.fulfill({ json: armed }))
    await page.route('**/api/arming/disarm', (route) => route.fulfill({ json: { ok: false, error } }))
    await page.goto('/strategies')
    const region = page.getByRole('region', { name: 'Armed accounts' })
    await region.getByRole('button', { name: 'Disarm Sim101' }).click()
    const dialog = page.getByRole('alertdialog', { name: 'Disarm Sim101' })
    await expect(dialog).toContainText("NinjaTrader flattens this account first: its open position is closed at market and the working orders on each instrument it flattens cancelled, Fenrir's among them; an order on any other instrument stays.")
    await dialog.getByRole('button', { name: 'Flatten and disarm', exact: true }).click() // never just 'Disarm'
    await expect(dialog.getByRole('alert')).toHaveText(`Sim101: ${error}.`)
    // #230: only an unmanaged_account refusal offers a disarm without flatten
    await expect(dialog.getByRole('button', { name: 'Disarm without flatten' })).toHaveCount(0)
    await expect(dialog.getByRole('checkbox')).toHaveCount(0)
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(region).toContainText('Sim101') // still armed, halted
  })
}

test('an account the add-on no longer manages can be disarmed without flatten, only after a ticked second confirmation (#230)', async ({ page }) => {
  await mockApi(page)
  const disarms: unknown[] = []
  let armed = [{ account: 'Sim101', strategy_path: STRATEGY, strategy_hash: 'a'.repeat(64), rules_account: 'lucid-flex-50k', contract: 'MNQ', mode: 'live', phase: 'evaluation', armed_at: '2026-10-13T13:30:00+00:00', dll_on: false, rules_hash: 'b'.repeat(64), instruments_hash: 'c'.repeat(64), start_balance: 50000, peak_balance: 50500 }]
  const error = 'disarm failed: flatten failed: unmanaged_account; the position may still be open: check NinjaTrader; still armed, halted until disarmed'
  await page.route('**/api/arming', (route) => route.fulfill({ json: armed }))
  await page.route('**/api/arming/disarm', (route) => {
    const body = route.request().postDataJSON()
    disarms.push(body)
    if (body.flatten !== false) return route.fulfill({ json: { ok: false, error, unmanaged: true } })
    armed = []
    return route.fulfill({ json: { ok: true, error: null } })
  })
  await page.goto('/strategies')
  const region = page.getByRole('region', { name: 'Armed accounts' })
  await region.getByRole('button', { name: 'Disarm Sim101' }).click()
  const dialog = page.getByRole('alertdialog', { name: 'Disarm Sim101' })
  await expect(dialog.getByRole('button', { name: 'Disarm without flatten' })).toHaveCount(0) // not before the refusal
  await dialog.getByRole('button', { name: 'Flatten and disarm', exact: true }).click()
  await expect(dialog.getByRole('alert')).toHaveText(`Sim101: ${error}.`)
  await expect(dialog).toContainText("NinjaTrader's Fenrir add-on no longer manages Sim101, so Fenrir can neither see nor flatten it there.")
  await expect(dialog).toContainText('any open position or working order on Sim101 is yours to close in NinjaTrader.')
  const without = dialog.getByRole('button', { name: 'Disarm without flatten' })
  await expect(without).toBeDisabled()
  const tick = dialog.getByRole('checkbox', { name: 'NinjaTrader no longer manages Sim101, and I will close any position or working order there in NinjaTrader myself.' })
  await tick.click()
  await expect(without).toBeEnabled()
  await tick.click() // unticked again: disabled again
  await expect(without).toBeDisabled()
  expect(disarms).toEqual([{ account: 'Sim101' }])
  await tick.click()
  await without.click()
  await expect(dialog).toBeHidden()
  expect(disarms).toEqual([{ account: 'Sim101' }, { account: 'Sim101', flatten: false }])
  await expect(region).toContainText('Nothing armed.')
})

test('cancelling the second confirmation sends nothing more, and a new request or reopening asks from the start (#230)', async ({ page }) => {
  await mockApi(page)
  const disarms: unknown[] = []
  const armed = [{ account: 'Sim101', strategy_path: STRATEGY, strategy_hash: 'a'.repeat(64), rules_account: 'lucid-flex-50k', contract: 'MNQ', mode: 'sim', phase: 'evaluation', armed_at: '2026-10-13T13:30:00+00:00', dll_on: false, rules_hash: 'b'.repeat(64), instruments_hash: 'c'.repeat(64), start_balance: 50000, peak_balance: 50500 }]
  const error = 'disarm failed: flatten failed: unmanaged_account; the position may still be open: check NinjaTrader; still armed, halted until disarmed'
  await page.route('**/api/arming', (route) => route.fulfill({ json: armed }))
  await page.route('**/api/arming/disarm', (route) => {
    disarms.push(route.request().postDataJSON())
    return route.fulfill({ json: { ok: false, error, unmanaged: true } })
  })
  await page.goto('/strategies')
  const region = page.getByRole('region', { name: 'Armed accounts' })
  await region.getByRole('button', { name: 'Disarm Sim101' }).click()
  const dialog = page.getByRole('alertdialog', { name: 'Disarm Sim101' })
  const flattenAndDisarm = dialog.getByRole('button', { name: 'Flatten and disarm', exact: true })
  const without = dialog.getByRole('button', { name: 'Disarm without flatten' })
  const tick = dialog.getByRole('checkbox')
  await flattenAndDisarm.click()
  await tick.click()
  await expect(without).toBeEnabled()
  // a new request clears the tick: refused as unmanaged again, it asks again
  await flattenAndDisarm.click()
  await expect.poll(() => disarms.length).toBe(2)
  await expect(tick).not.toBeChecked()
  await expect(without).toBeDisabled()
  await tick.click()
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toBeHidden()
  await region.getByRole('button', { name: 'Disarm Sim101' }).click()
  await expect(without).toHaveCount(0)
  await expect(tick).toHaveCount(0)
  expect(disarms).toEqual([{ account: 'Sim101' }, { account: 'Sim101' }])
  // refused again after reopening: the tick from before the cancel is gone
  await flattenAndDisarm.click()
  await expect(without).toBeDisabled()
  await expect(tick).not.toBeChecked()
  expect(disarms).toEqual([{ account: 'Sim101' }, { account: 'Sim101' }, { account: 'Sim101' }])
})

test('a disarm the server reports as not armed says so, and closing refreshes the table', async ({ page }) => {
  await mockApi(page)
  let armed = [{ account: 'LDA0042', strategy_path: STRATEGY, strategy_hash: 'a'.repeat(64), rules_account: 'lucid-flex-50k', contract: 'MNQ', mode: 'dry', phase: 'evaluation', armed_at: '2026-10-13T13:30:00+00:00', dll_on: false, rules_hash: 'b'.repeat(64), instruments_hash: 'c'.repeat(64), start_balance: null, peak_balance: null }]
  await page.route('**/api/arming', (route) => route.fulfill({ json: armed }))
  await page.route('**/api/arming/disarm', (route) => {
    armed = [] // the kill switch got there first
    return route.fulfill({ json: { ok: false, error: null } })
  })
  await page.goto('/strategies')
  const region = page.getByRole('region', { name: 'Armed accounts' })
  await region.getByRole('button', { name: 'Disarm LDA0042' }).click()
  const dialog = page.getByRole('alertdialog', { name: 'Disarm LDA0042' })
  await dialog.getByRole('button', { name: 'Disarm', exact: true }).click()
  await expect(dialog.getByRole('alert')).toHaveText('LDA0042 was not armed: nothing was disarmed.')
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(region).toContainText('Nothing armed.')
})
