import { type Page, type Route, expect, test } from '@playwright/test'
import { account, desk, deskJournal, status } from '../src/live/fixtures'
import type { LiveStatus } from '../src/live/model'

// Set by global-setup.ts after it opened the launch link.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

/** Mocks `GET /api/live/status`, `GET /api/live/journal` (by account) and `POST /api/live/resolve` (the real hub
 * never runs here); records the calls. */
async function mock(page: Page, live: LiveStatus, resolve: (route: Route) => Promise<void> = (r) => r.fulfill({ json: { ok: true } })) {
  const calls = { status: 0, journal: [] as [string | null, string | null][], resolve: [] as unknown[] }
  const journal = deskJournal()
  await page.route('**/api/live/status', (route) => {
    calls.status++
    return route.fulfill({ json: live })
  })
  await page.route('**/api/live/journal?*', (route) => {
    const url = new URL(route.request().url())
    calls.journal.push([url.searchParams.get('account'), url.searchParams.get('day')])
    return route.fulfill({ json: journal[url.searchParams.get('account') ?? ''] ?? [] })
  })
  await page.route('**/api/live/resolve', (route) => {
    expect(route.request().method()).toBe('POST')
    calls.resolve.push(route.request().postDataJSON())
    return resolve(route)
  })
  return calls
}

const focus = (page: Page) => page.getByRole('region', { name: 'Focused account' })

test('the most severe account is focused; a sleeve click focuses another', async ({ page }) => {
  await mock(page, desk())
  await page.goto('/live')
  // LFE 0911 has a book mismatch (alarm): it outranks the safety halt (caution) and the healthy accounts
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0911')
  await expect(focus(page).getByRole('img', { name: 'Risk normal, book alarm' })).toBeVisible()
  await expect(focus(page).getByRole('img', { name: /^Session dial: today \+\$96, floor −\$3,000/ })).toBeVisible()
  await expect(focus(page).getByRole('button', { name: 'Resolve LFE 0911' })).toBeVisible()

  const sleeves = page.getByRole('article')
  await expect(sleeves).toHaveCount(3)
  await expect(page.getByRole('article', { name: 'LPR 1123' })).toHaveCSS('box-shadow', /rgb\(224, 163, 58\) 0px 0px 0px 1px inset$/)
  await page.getByRole('article', { name: 'LFE 0507' }).click()
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0507')
  await expect(focus(page).getByText('Long 2 @ 25,412.25').first()).toBeVisible()
  await expect(focus(page).getByRole('button', { name: /^Resolve/ })).toHaveCount(0)
  // the alarm account is now a sleeve, outlined in Red, with its own Resolve
  const book = page.getByRole('article', { name: 'LFE 0911' })
  await expect(book).toHaveCSS('box-shadow', /rgb\(229, 72, 77\) 0px 0px 0px 1px inset$/)
  await expect(book.getByRole('button', { name: 'Resolve LFE 0911' })).toBeVisible()
})

test('the meter safety mark is an inset Ember edge, and cells past it are Ember', async ({ page }) => {
  await mock(page, desk())
  await page.goto('/live')
  await page.getByRole('button', { name: 'Focus LPR 1123' }).click()
  const daily = focus(page).getByRole('img', { name: 'Daily loss left: 81% used, safety mark at 80%' })
  await expect(daily).toBeVisible()
  const mark = daily.locator('[data-mark]')
  await expect(mark).toHaveCount(1)
  await expect(mark).toHaveCSS('box-shadow', 'rgb(224, 163, 58) 0px 3px 0px 0px inset')
  await expect(daily.locator('[data-state="caution"]')).toHaveCount(16)
  await expect(focus(page).getByRole('img', { name: 'Session dial: today −$812, floor −$2,500, safety −$628, start $172' })).toBeVisible()
  // the safety ring turns Ember when equity is at or below it
  await expect(focus(page).locator('[data-ring="safety"]')).toHaveCSS('stroke', 'rgb(224, 163, 58)')
})

test('the dial trace draws itself, and not under reduced motion', async ({ page }) => {
  await mock(page, desk())
  await page.goto('/live')
  const trace = focus(page).locator('[data-trace="live"]')
  await expect(trace).toHaveCSS('animation-name', 'trace')
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.reload()
  await expect(focus(page).locator('[data-trace="live"]')).toHaveCSS('animation-name', 'none')
})

test("the journal tape shows today's events, MISMATCH in Red, and links to the full journal", async ({ page }) => {
  const calls = await mock(page, desk())
  await page.goto('/live')
  const tape = page.getByRole('region', { name: 'Journal · today' })
  const rows = tape.getByRole('listitem')
  await expect(rows.first()).toContainText('MISMATCHLFE 0911 · position mismatch: NinjaTrader long 1, runner flat')
  await expect(rows.first().getByText('MISMATCH')).toHaveCSS('color', 'rgb(229, 72, 77)')
  await expect(tape.locator('li[data-kind="HALT"]').getByText('HALT')).toHaveCSS('color', 'rgb(224, 163, 58)')
  await expect(tape.locator('li[data-kind="REJECT"]').getByText('REJECT')).toHaveCSS('color', 'rgb(143, 160, 178)')
  await expect(tape.locator('li[data-kind="BAR"]')).toHaveCount(0) // bars only in the full journal
  await expect(rows).toHaveCount(7)
  // one read per armed account, for today's trading day in New York
  expect(new Set(calls.journal.map(([a]) => a))).toEqual(new Set(['LFE 0507', 'LPR 1123', 'LFE 0911', 'LDA 0042']))
  expect(calls.journal[0]?.[1]).toMatch(/^\d{4}-\d{2}-\d{2}$/)

  await tape.getByRole('link', { name: 'Full journal' }).click()
  await expect(page).toHaveURL(/\/live\/journal$/)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Journal')
  await expect(page.getByRole('main').getByRole('listitem')).toHaveCount(9)
  await expect(page.locator('li[data-kind="BAR"]')).toContainText('LFE 0507 · MNQZ26 C 25,418.50 · hold')
})

test('Resolve posts the account, says it ran and re-reads the status', async ({ page }) => {
  const calls = await mock(page, desk())
  await page.goto('/live')
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0911')
  const before = calls.status
  await focus(page).getByRole('button', { name: 'Resolve LFE 0911' }).click()
  await expect(page.getByRole('status')).toHaveText(/^Resolve ran for LFE 0911: pauses lifted/)
  expect(calls.resolve).toEqual([{ account: 'LFE 0911' }])
  await expect.poll(() => calls.status).toBeGreaterThan(before)
})

test('Resolve shows for a drift pause too; a 404 says not running, a 500 re-reads the status', async ({ page }) => {
  const drift = status({
    drift: { ok: false, ticks: 9 },
    accounts: [account({ account: 'LFE 0612', mode: 'sim', halted: true, halt_kind: 'drift', halt_reason: 'drift: fills 9 ticks from the LSE close' })],
  })
  let reply = 404
  const calls = await mock(page, drift, (route) => route.fulfill({ status: reply, json: { detail: "'LFE 0612' is not running" } }))
  await page.goto('/live')
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0612')
  const button = focus(page).getByRole('button', { name: 'Resolve LFE 0612' })
  await button.click()
  await expect(page.getByRole('alert')).toHaveText('LFE 0612 is not running: nothing to resolve.')
  await expect(page.getByRole('alert')).not.toContainText('armed')

  reply = 500
  const before = calls.status
  await button.click()
  await expect(page.getByRole('alert')).toHaveText(/^Resolve for LFE 0612 failed on the server \(500\): the pause may already be lifted/)
  await expect(page.getByRole('alert')).toHaveCSS('color', 'rgb(229, 72, 77)')
  await expect.poll(() => calls.status).toBeGreaterThan(before)
  expect(calls.resolve).toHaveLength(2)
})

test('when the status read fails, the focus field, sleeves and tape go rather than show stale figures', async ({ page }) => {
  await mock(page, desk())
  await page.goto('/live')
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0911')
  await page.unroute('**/api/live/status')
  await page.route('**/api/live/status', (route) => route.fulfill({ status: 500, body: 'Internal Server Error' }))
  await expect(page.getByText('Accounts unavailable until the status reads again.')).toBeVisible()
  await expect(focus(page)).toHaveCount(0)
  await expect(page.getByRole('article')).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Journal · today' })).toHaveCount(0)
})
