import { type Page, type Route, expect, test } from '@playwright/test'
import { account, status } from '../src/live/fixtures'
import type { KillResult } from '../src/live/model'

// Set by global-setup.ts after it opened the launch link.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

type Reply = (route: Route) => Promise<void>
const answer =
  (kill: KillResult): Reply =>
  (route) =>
    route.fulfill({ json: kill })

/** Mocks `GET /api/live/status` and `POST /api/live/kill` (the real hub never runs a kill here); counts the calls. */
async function mock(page: Page, statusReply: Reply, killReply: Reply = answer({ ok: true, error: null })) {
  const calls = { status: 0, kill: 0 }
  await page.route('**/api/live/status', (route) => {
    calls.status++
    return statusReply(route)
  })
  await page.route('**/api/live/kill', (route) => {
    calls.kill++
    expect(route.request().method()).toBe('POST')
    return killReply(route)
  })
  return calls
}

const two = status({
  accounts: [
    account({ position: { qty: 2, entry: 25412.25, stop: 25402.25, target: 25432.25 } }),
    account({ account: 'LFE 0911', mode: 'sim', halted: true, halt_kind: 'reconcile', halt_reason: 'NT long 1, runner flat' }),
  ],
})
const ok: Reply = (route) => route.fulfill({ json: two })
const fail: Reply = (route) => route.fulfill({ status: 500, body: 'Internal Server Error' })

const killAll = (page: Page) => page.getByRole('button', { name: /^(Kill all|Killing…)$/ })
const confirm = (page: Page) => page.getByRole('alertdialog').getByRole('button', { name: 'Flatten and disarm all' })
const TIME = String.raw`\d{2}:\d{2}:\d{2}\s*`

test('the kill switch asks first in a Red-bordered dialog, and Cancel sends nothing', async ({ page }) => {
  const calls = await mock(page, ok)
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

test('confirming posts the kill once, says when it ran and re-reads the status', async ({ page }) => {
  const calls = await mock(page, ok)
  await page.goto('/live')
  await expect(page.getByRole('img', { name: /^Feed / })).toBeVisible()

  await killAll(page).click()
  const before = calls.status
  await confirm(page).click()
  await expect(page.getByRole('alertdialog')).toBeHidden()
  await expect(page.getByRole('status')).toHaveText(new RegExp(`^${TIME}Kill switch ran: flattened and cancelled all, every account disarmed\\.$`))
  await expect.poll(() => calls.status).toBeGreaterThan(before)
  expect(calls.kill).toBe(1)
  await expect(killAll(page)).toHaveText('Kill all')
  await expect(killAll(page)).toBeEnabled()
})

test('with the status failing (500), the kill switch still works, and a failed flatten shows as an alarm', async ({ page }) => {
  const calls = await mock(page, fail, answer({ ok: false, error: 'flatten all failed: ConnectError' }))
  await page.goto('/live')
  await expect(page.getByText('Status unavailable')).toBeVisible()
  await expect(page.getByRole('alert')).toHaveText('Status: 500: Internal Server Error')

  await killAll(page).click()
  const dialog = page.getByRole('alertdialog', { name: 'Kill all accounts' })
  await expect(dialog).toContainText('the counts are unknown.')
  await confirm(page).click()

  const alarm = page.getByRole('alert').filter({ hasText: 'Kill switch failed' })
  await expect(alarm).toHaveText(
    new RegExp(
      `^${TIME}Kill switch failed: flatten all failed: ConnectError\\. Accounts may already be disarmed or halted; check NinjaTrader for open positions and working orders\\.$`,
    ),
  )
  await expect(alarm).toHaveCSS('color', 'rgb(229, 72, 77)')
  expect(calls.kill).toBe(1)
})

test('while the kill runs Kill all stays disabled, even across a page change, and a failed request says it may not have run', async ({ page }) => {
  let release!: () => void
  const held = new Promise<void>((r) => (release = r))
  const calls = await mock(page, ok, async (route) => {
    await held
    await fail(route)
  })
  await page.goto('/live')
  await killAll(page).click()
  await confirm(page).click()
  await expect(killAll(page)).toHaveText('Killing…')
  await expect(killAll(page)).toBeDisabled()
  await expect(page.getByRole('status')).toContainText('Kill switch running')

  const rail = page.getByRole('navigation', { name: 'Pages' })
  await rail.getByRole('link', { name: 'Data', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Data')
  await rail.getByRole('link', { name: 'Live', exact: true }).click()
  await expect(killAll(page)).toHaveText('Killing…')
  await expect(killAll(page)).toBeDisabled()

  release()
  await expect(page.getByRole('alert').filter({ hasText: 'Kill switch' })).toHaveText(
    new RegExp(`^${TIME}Kill switch request failed \\(500: Internal Server Error\\)\\. It may not have run: check NinjaTrader and press it again\\.$`),
  )
  await expect(killAll(page)).toBeEnabled()
  expect(calls.kill).toBe(1)
})

test('the kill POST gives up after 90 s and says it may not have run', async ({ page }) => {
  // The page's AbortSignal.timeout(ms) is recorded and fires after 50 ms instead, so the test need not wait 90 s.
  await page.addInitScript(() => {
    const timeout = AbortSignal.timeout.bind(AbortSignal)
    const asked: number[] = ((window as unknown as { asked: number[] }).asked = [])
    AbortSignal.timeout = (ms: number) => {
      asked.push(ms)
      return timeout(50)
    }
  })
  let release!: () => void
  const held = new Promise<void>((r) => (release = r))
  const calls = await mock(page, ok, async (route) => {
    await held // never answered: only the page's own timeout ends the request
    await route.abort().catch(() => {}) // the page has already aborted it
  })
  await page.goto('/live')
  await killAll(page).click()
  await confirm(page).click()
  await expect(page.getByRole('alert').filter({ hasText: 'Kill switch' })).toHaveText(
    new RegExp(`^${TIME}Kill switch got no answer in 90 s\\. It may not have run: check NinjaTrader and press it again\\.$`),
  )
  expect(await page.evaluate(() => (window as unknown as { asked: number[] }).asked)).toContain(90_000)
  await expect(killAll(page)).toBeEnabled()
  expect(calls.kill).toBe(1)
  release()
})

test('the status polls every 2 s with no retry, and a failed re-read is never shown as current', async ({ page }) => {
  await page.clock.install()
  const calls = await mock(page, (route) => (calls.status === 1 ? ok(route) : fail(route)))
  await page.goto('/live')
  await expect(page.getByText('Book · LFE 0911')).toBeVisible()
  expect(calls.status).toBe(1)

  await page.clock.runFor(1900) // a retry would have gone out after 1 s
  expect(calls.status).toBe(1)
  await page.clock.runFor(200)
  await expect.poll(() => calls.status).toBe(2)

  await expect(page.getByText('Status unavailable')).toBeVisible()
  await expect(page.getByRole('img', { name: /^Feed / })).toBeHidden()
  await expect(page.getByText('Book · LFE 0911')).toBeHidden()
  await killAll(page).click()
  await expect(page.getByRole('alertdialog')).toContainText('the counts are unknown.')
  await expect(page.getByRole('alertdialog').getByRole('listitem')).toHaveCount(0)
})

for (const [width, height] of [
  [1280, 800],
  [1024, 768],
  [800, 900],
  [390, 844],
] as const) {
  test(`Kill all is in view with no sideways scroll at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height })
    await mock(page, ok)
    await page.goto('/live')
    await expect(page.getByText('Book · LFE 0911')).toBeVisible()
    await expect(killAll(page)).toBeInViewport({ ratio: 1 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
  })
}
