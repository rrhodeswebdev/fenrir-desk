import { type Page, type Route, expect, test } from '@playwright/test'
import { account, desk, deskJournal, held as under, status } from '../src/live/fixtures'
import type { LiveStatus } from '../src/live/model'

// Set by global-setup.ts after it opened the launch link.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

/** Mocks `GET /api/live/status`, `GET /api/live/journal` (every account's rows, by id, honouring `skip`) and
 * `POST /api/live/resolve` (the real hub never runs here); records the calls. */
async function mock(page: Page, live: LiveStatus, resolve: (route: Route) => Promise<void> = (r) => r.fulfill({ json: { ok: true } })) {
  const calls = { status: 0, journal: [] as URLSearchParams[], resolve: [] as unknown[] }
  const journal = Object.values(deskJournal())
    .flat()
    .sort((x, y) => x.id - y.id)
  await page.route('**/api/live/status', (route) => {
    calls.status++
    return route.fulfill({ json: live })
  })
  await page.route('**/api/live/journal?*', (route) => {
    const q = new URL(route.request().url()).searchParams
    calls.journal.push(q)
    return route.fulfill({ json: journal.filter((r) => !q.getAll('skip').includes(r.kind)) })
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
  await expect(focus(page).getByRole('img', { name: /^Session dial: today \+\$96, floor −\$2,676/ })).toBeVisible()
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
  // a visible way back to automatic focus
  await focus(page).getByRole('button', { name: 'Follow most severe' }).click()
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0911')
  await expect(focus(page).getByRole('button', { name: 'Follow most severe' })).toHaveCount(0)
})

test('a new alarm takes focus from a picked account', async ({ page }) => {
  const calm = desk()
  calm.accounts = calm.accounts.map((a) => (a.account === 'LFE 0911' ? { ...a, ...under() } : a))
  let live = calm
  await page.route('**/api/live/status', (route) => route.fulfill({ json: live }))
  await page.route('**/api/live/journal?*', (route) => route.fulfill({ json: [] }))
  await page.goto('/live')
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LPR 1123') // the safety halt: most severe
  await page.getByRole('button', { name: 'Focus LDA 0042' }).click()
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LDA 0042')
  live = desk() // LFE 0911 goes into alarm
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0911')
  // the takeover is held as the pick, until a further alarm; the way back to automatic focus stays
  await expect(focus(page).getByRole('button', { name: 'Follow most severe' })).toBeVisible()

  // pick LDA 0042 while LFE 0911 is in alarm; then LDA 0042 itself alarms: focus goes to it, not back to LFE 0911
  // (a sim alarm outranks a dry run's, so "most severe of all" would have been LFE 0911)
  await page.getByRole('button', { name: 'Focus LDA 0042' }).click()
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LDA 0042')
  const next = desk()
  next.accounts[3] = { ...next.accounts[3]!, ...under({ kind: 'reconcile', reason: 'position mismatch' }) }
  live = next
  await expect(page.getByRole('article', { name: 'LFE 0911' })).toBeVisible()
  await expect(focus(page).getByRole('img', { name: 'Risk normal, book alarm' })).toBeVisible()
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LDA 0042')
})

test('a mismatch under a safety halt is a Book alarm; a stale pause under a halt lights Feed (#212)', async ({ page }) => {
  const live = desk()
  // LPR 1123 is on a safety halt; NinjaTrader's book then disagrees and the feed goes quiet
  live.accounts[1] = {
    ...live.accounts[1]!,
    ...under(
      { kind: 'safety', reason: 'safety margin: 81% of the daily loss used' },
      { kind: 'reconcile', reason: 'NinjaTrader mismatch: stray_orders: o1; fix it there, then resolve' },
      { kind: 'stale', reason: 'no NQ bar for 191s; new entries paused' },
    ),
  }
  live.accounts[2] = { ...live.accounts[2]!, ...under() } // LFE 0911's own mismatch is resolved
  await mock(page, live)
  await page.goto('/live')
  await expect(page.getByRole('img', { name: 'Feed caution, broker normal, drift normal' })).toBeVisible()
  await expect(page.getByText('Book · LPR 1123')).toBeVisible()
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LPR 1123')
  await expect(focus(page).getByRole('img', { name: 'Risk caution, book alarm' })).toBeVisible()
  await expect(focus(page).getByText('NinjaTrader mismatch: stray_orders: o1; fix it there, then resolve')).toHaveCSS('color', 'rgb(229, 72, 77)')
  await expect(focus(page).getByText('safety margin: 81% of the daily loss used')).toBeVisible()
  await expect(focus(page).getByRole('button', { name: 'Resolve LPR 1123' })).toBeVisible()
})

test('the strip says why the LSE feed is down and when it is retried', async ({ page }) => {
  const live = desk()
  const error = 'LSE stream stopped: Max 1 concurrent websocket connection(s) for this API key'
  live.lse = { connected: false, last_bar_ts: '2026-10-13T18:31:00+00:00', error, retry_at: '2099-01-02T15:04:05+00:00' }
  await mock(page, live)
  await page.goto('/live')
  const feed = page.getByRole('group', { name: 'Connections' })
  await expect(page.getByRole('img', { name: /^Feed alarm/ })).toBeVisible()
  await expect(feed.getByText('Retry 10:04:05')).toHaveCSS('color', 'rgb(229, 72, 77)')
  await expect(feed.getByText(error)).toBeVisible()
  // the same strip on the full journal
  await page.getByRole('link', { name: 'Full journal' }).click()
  await expect(page.getByRole('group', { name: 'Connections' }).getByText(error)).toBeVisible()
  live.lse = { ...live.lse, retry_at: null }
  await page.reload()
  await expect(page.getByRole('group', { name: 'Connections' }).getByText('Restart needed')).toBeVisible()
})

test('the focused dial redraws on a focus change, not on every status poll', async ({ page }) => {
  const calls = await mock(page, desk())
  await page.goto('/live')
  const trace = await focus(page).locator('[data-trace="live"]').elementHandle()
  const before = calls.status
  await expect.poll(() => calls.status).toBeGreaterThan(before + 1)
  expect(await trace!.evaluate((e) => e.isConnected)).toBe(true) // same path: no new draw
  await page.getByRole('button', { name: 'Focus LFE 0507' }).click()
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0507')
  expect(await trace!.evaluate((e) => e.isConnected)).toBe(false) // a new path: the trace draws again
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
  await expect(focus(page).getByRole('img', { name: 'Session dial: today −$812, floor −$2,500, safety −$628, open +$172' })).toBeVisible()
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
  // one read for every account (disarmed ones too), today's trading day in New York, bars, peaks and opens left on the server
  const q = calls.journal[0]!
  expect(q.get('account')).toBeNull()
  expect(q.get('day')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  expect(q.getAll('skip')).toEqual(['bar', 'peak', 'day_start'])

  await tape.getByRole('link', { name: 'Full journal' }).click()
  await expect(page).toHaveURL(/\/live\/journal$/)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Journal')
  // bars stay on the server until asked for
  await expect(page.getByRole('main').getByRole('listitem')).toHaveCount(9)
  await expect(page.locator('li[data-kind="OPEN"]')).toContainText('LFE 0507 · open $51,200 · peak $51,412')
  expect(calls.journal.at(-1)?.getAll('skip')).toEqual(['bar'])
  await page.getByRole('button', { name: 'Show bars' }).click()
  await expect(page.getByRole('main').getByRole('listitem')).toHaveCount(10)
  await expect(page.getByRole('button', { name: 'Hide bars' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.locator('li[data-kind="BAR"]')).toContainText('LFE 0507 · MNQZ26 C 25,418.50 · hold')
  expect(calls.journal.at(-1)?.getAll('skip')).toEqual([])
  // the full journal is a Live page: the system strip and Kill all stay in view
  await expect(page.getByRole('img', { name: 'Feed normal, broker normal, drift normal' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Kill all' })).toBeInViewport()
})

test('Resolve posts the account, says it ran and re-reads the status', async ({ page }) => {
  const calls = await mock(page, desk())
  await page.goto('/live')
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0911')
  const before = calls.status
  await focus(page).getByRole('button', { name: 'Resolve LFE 0911' }).click()
  await expect(page.getByRole('status')).toHaveText(/^\d{2}:\d{2}:\d{2}Resolve ran for LFE 0911: pauses lifted/)
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
  await expect(page.getByRole('alert')).toHaveText(/^\d{2}:\d{2}:\d{2}LFE 0612 is not running: nothing to resolve\.$/)
  await expect(page.getByRole('alert')).not.toContainText('armed')

  reply = 500
  const before = calls.status
  await button.click()
  await expect(page.getByRole('alert')).toHaveText(/^\d{2}:\d{2}:\d{2}Resolve for LFE 0612 failed on the server \(500\): the pause may already be lifted/)
  await expect(page.getByRole('alert')).toHaveCSS('color', 'rgb(229, 72, 77)')
  await expect.poll(() => calls.status).toBeGreaterThan(before)
  expect(calls.resolve).toHaveLength(2)
})

test('while Resolve runs every Resolve is disabled, also after leaving the page and coming back', async ({ page }) => {
  let release = () => {}
  const held = new Promise<void>((r) => (release = r))
  const two = desk() // LDA 0042 on a drift pause: a second Resolve, on its sleeve
  two.accounts[3] = { ...two.accounts[3]!, ...under({ kind: 'drift', reason: 'drift: 9 ticks' }) }
  await mock(page, two, async (route) => {
    await held
    await route.fulfill({ json: { ok: true } })
  })
  await page.goto('/live')
  await focus(page).getByRole('button', { name: 'Resolve LFE 0911' }).click()
  await expect(focus(page).getByRole('button', { name: 'Resolve LFE 0911' })).toBeDisabled()
  await expect(focus(page).getByRole('button', { name: 'Resolve LFE 0911' })).toHaveText('Resolving…')
  await expect(page.getByRole('button', { name: 'Resolve LDA 0042' })).toBeDisabled()
  await expect(page.getByRole('status')).toContainText('Resolve running for LFE 0911')
  await page.getByRole('link', { name: 'Full journal' }).click()
  await page.getByRole('link', { name: 'Back to Live' }).click()
  await expect(focus(page).getByRole('button', { name: 'Resolve LFE 0911' })).toHaveText('Resolving…')
  await expect(focus(page).getByRole('button', { name: 'Resolve LFE 0911' })).toBeDisabled()
  release()
  await expect(page.getByRole('status')).toHaveText(/Resolve ran for LFE 0911/)
  await expect(focus(page).getByRole('button', { name: 'Resolve LFE 0911' })).toBeEnabled()
})

test('a failed journal read shows unavailable, never the last rows', async ({ page }) => {
  await mock(page, desk())
  await page.goto('/live')
  const tape = page.getByRole('region', { name: 'Journal · today' })
  await expect(tape.getByRole('listitem')).toHaveCount(7)
  await page.unroute('**/api/live/journal?*')
  await page.route('**/api/live/journal?*', (route) => route.fulfill({ status: 500, body: 'Internal Server Error' }))
  await expect(tape.getByRole('alert')).toHaveText('Journal: 500: Internal Server Error', { timeout: 10_000 })
  await expect(tape.getByRole('listitem')).toHaveCount(0)
})

test('when the status read fails, the focus field and sleeves go rather than show stale figures', async ({ page }) => {
  await mock(page, desk())
  await page.goto('/live')
  await expect(focus(page).getByRole('heading', { level: 2 })).toHaveText('LFE 0911')
  await page.unroute('**/api/live/status')
  await page.route('**/api/live/status', (route) => route.fulfill({ status: 500, body: 'Internal Server Error' }))
  await expect(page.getByText('Accounts unavailable until the status reads again.')).toBeVisible()
  await expect(focus(page)).toHaveCount(0)
  await expect(page.getByRole('article')).toHaveCount(0)
  // the tape reads the journal on its own: it stays
  await expect(page.getByRole('region', { name: 'Journal · today' }).getByRole('listitem')).toHaveCount(7)
})

test('with nothing armed (after a kill) the tape still shows today, disarmed accounts included', async ({ page }) => {
  await mock(page, status({ accounts: [] }))
  await page.goto('/live')
  await expect(page.getByText('No account is armed.')).toBeVisible()
  await expect(page.getByRole('region', { name: 'Journal · today' }).getByRole('listitem').first()).toContainText('MISMATCHLFE 0911')
})
