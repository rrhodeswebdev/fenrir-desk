import { type Page, type Route, expect, test } from '@playwright/test'
import { account, status } from '../src/live/fixtures'
import type { KillResult } from '../src/live/model'

// Set by global-setup.ts after it opened the launch link.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

/** Mocks `GET /api/live/status` and `POST /api/live/kill` (the real hub never runs a kill here); counts the calls. */
async function mock(page: Page, statusReply: (route: Route) => Promise<void>, kill: KillResult = { ok: true, error: null }) {
  const calls = { status: 0, kill: 0 }
  await page.route('**/api/live/status', (route) => {
    calls.status++
    return statusReply(route)
  })
  await page.route('**/api/live/kill', (route) => {
    calls.kill++
    expect(route.request().method()).toBe('POST')
    return route.fulfill({ json: kill })
  })
  return calls
}

const two = status({
  accounts: [
    account({ position: { qty: 2, entry: 25412.25, stop: 25402.25, target: 25432.25 } }),
    account({ account: 'LFE 0911', mode: 'sim', halted: true, halt_kind: 'reconcile', halt_reason: 'NT long 1, runner flat' }),
  ],
})

const killAll = (page: Page) => page.getByRole('button', { name: 'Kill all' })

test('the kill switch asks first in a Red-bordered dialog, and Cancel sends nothing', async ({ page }) => {
  const calls = await mock(page, (route) => route.fulfill({ json: two }))
  await page.goto('/live')
  await expect(page.getByRole('img', { name: 'Feed normal, broker normal, drift normal' })).toBeVisible()
  await expect(page.getByText('Book · LFE 0911')).toBeVisible()

  await killAll(page).click()
  const dialog = page.getByRole('alertdialog', { name: 'Kill all accounts' })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('Flatten every position, cancel every working order, disarm every account.')
  await expect(dialog.getByRole('listitem')).toHaveText(['2 accounts armed', '1 open position'])
  await expect(dialog).toHaveCSS('border-top-color', 'rgb(229, 72, 77)')
  await expect(dialog).toHaveCSS('border-radius', '0px')
  await expect(dialog).toHaveCSS('box-shadow', 'none')
  // the safe choice has focus, so a stray Enter does not kill
  await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()

  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toBeHidden()
  expect(calls.kill).toBe(0)
})

test('confirming posts the kill once, says it ran and re-reads the status', async ({ page }) => {
  const calls = await mock(page, (route) => route.fulfill({ json: two }))
  await page.goto('/live')
  await expect(page.getByRole('img', { name: /^Feed / })).toBeVisible()

  await killAll(page).click()
  const before = calls.status
  await page.getByRole('alertdialog').getByRole('button', { name: 'Flatten and disarm all' }).click()
  await expect(page.getByRole('alertdialog')).toBeHidden()
  await expect(page.getByRole('status')).toHaveText('Kill switch ran: flattened and cancelled all, every account disarmed.')
  await expect.poll(() => calls.status).toBeGreaterThan(before)
  expect(calls.kill).toBe(1)
  await expect(killAll(page)).toBeEnabled()
})

test('with the status failing (500), the kill switch still works, and a failed flatten shows as an alarm', async ({ page }) => {
  const calls = await mock(
    page,
    (route) => route.fulfill({ status: 500, body: 'Internal Server Error' }),
    { ok: false, error: 'flatten all failed: ConnectError' },
  )
  await page.goto('/live')
  await expect(page.getByText('Status unavailable')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveText('Status: 500: Internal Server Error')

  await killAll(page).click()
  const dialog = page.getByRole('alertdialog', { name: 'Kill all accounts' })
  await expect(dialog).toContainText('The status did not load, so the counts are unknown.')
  await dialog.getByRole('button', { name: 'Flatten and disarm all' }).click()

  const alarm = page.getByRole('alert').filter({ hasText: 'Kill switch failed' })
  await expect(alarm).toHaveText(
    'Kill switch failed: flatten all failed: ConnectError. Accounts may already be disarmed or halted; check NinjaTrader for open positions and working orders.',
  )
  await expect(alarm).toHaveCSS('color', 'rgb(229, 72, 77)')
  expect(calls.kill).toBe(1)
})
