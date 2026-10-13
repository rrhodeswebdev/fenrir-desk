import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { account, desk, held, status } from './fixtures'
import {
  type LiveStatus,
  type Meter,
  KILL_TIMEOUT_MS,
  accountStrip,
  alertTone,
  bracketText,
  canResolve,
  feedDown,
  reasons,
  alarmsOf,
  focusOf,
  minis,
  pickOf,
  meterLabel,
  meters,
  positionText,
  resolveText,
  segments,
  typeText,
  usd,
  age,
  alarmName,
  killErrorText,
  killCounts,
  killText,
  mostSevere,
  need,
  nyTime,
  stripLabel,
  systemStrip,
} from './model'

const states = (s: LiveStatus) => systemStrip(s).map((c) => c.state)
const lseDown = { connected: false, last_bar_ts: null, error: null, retry_at: null }

describe('system strip', () => {
  it('is normal when the feed and the broker are up and nothing drifted', () => {
    expect(systemStrip(status())).toEqual([
      { name: 'Feed', state: 'normal' },
      { name: 'Broker', state: 'normal' },
      { name: 'Drift', state: 'normal' },
    ])
  })

  it('Feed: alarm when LSE is not connected, caution when an account has a stale pause', () => {
    expect(states(status({ lse: lseDown }))[0]).toBe('alarm')
    const stale = account({ halted: true, halt_kind: 'stale', halt_reason: 'no bar for 90 s' })
    expect(states(status({ accounts: [account(), stale] }))[0]).toBe('caution')
    // a lost feed outranks the pause it causes
    expect(states(status({ lse: lseDown, accounts: [stale] }))[0]).toBe('alarm')
    // a dry run steps on live bars too
    expect(states(status({ lse: lseDown, accounts: [account({ mode: 'dry' })] }))[0]).toBe('alarm')
  })

  it('Feed: idle with nothing armed, as nothing reads it', () => {
    expect(states(status({ lse: lseDown, accounts: [] }))[0]).toBe('idle')
    expect(states(status({ accounts: [] }))[0]).toBe('idle')
  })

  it('Broker: alarm when NinjaTrader is not connected, idle while every account is a dry run', () => {
    const down = { connected: false, last_heartbeat: null }
    expect(states(status({ nt: down }))[1]).toBe('alarm')
    expect(states(status({ nt: down, accounts: [account({ mode: 'dry' })] }))[1]).toBe('idle')
    expect(states(status({ nt: down, accounts: [] }))[1]).toBe('idle')
    expect(states(status({ nt: down, accounts: [account({ mode: 'dry' }), account({ mode: 'sim' })] }))[1]).toBe('alarm')
  })

  it('Drift: caution when drift is not ok', () => {
    expect(states(status({ drift: { ok: false, ticks: 6 } }))[2]).toBe('caution')
  })

  it('spells out each cell for the aria-label', () => {
    expect(stripLabel(systemStrip(status({ nt: { connected: false, last_heartbeat: null } })))).toBe(
      'Feed normal, broker alarm, drift normal',
    )
  })
})

describe('need', () => {
  it('Book alarm for a reconcile mismatch, Risk alarm for a breach, Halt for a halt no cell names', () => {
    expect(need(account({ halted: true, halt_kind: 'reconcile' }))).toEqual({ cell: 'Book', state: 'alarm' })
    expect(need(account({ halted: true, halt_kind: 'breach' }))).toEqual({ cell: 'Risk', state: 'alarm' })
    expect(need(account({ halted: true, halt_reason: 'strategy error: ValueError' }))).toEqual({ cell: 'Halt', state: 'alarm' })
  })

  it('Risk caution for a safety halt or equity at or below the safety level', () => {
    expect(need(account({ halted: true, halt_kind: 'safety' }))).toEqual({ cell: 'Risk', state: 'caution' })
    expect(need(account({ today_pnl: -400 }))).toEqual({ cell: 'Risk', state: 'caution' })
    expect(need(account({ today_pnl: -399.75 }))).toBeNull()
    const s = account().session!
    expect(need(account({ today_pnl: -150, session: { ...s, start: -250 } }))).toEqual({ cell: 'Risk', state: 'caution' })
  })

  it('nothing for stale and drift pauses (the system strip shows them), a healthy account, or the brief unreconciled pause', () => {
    expect(need(account({ halted: true, halt_kind: 'stale' }))).toBeNull()
    expect(need(account({ halted: true, halt_kind: 'drift' }))).toBeNull()
    expect(need(account())).toBeNull()
    expect(need(account({ halt_reason: 'unreconciled' }))).toBeNull()
  })

  it('an account that cannot start is a Halt, with no figures to read', () => {
    const broken = account({ halted: true, halt_reason: 'cannot start: boom', balance: null, today_pnl: null, limits: null, position: null, session: null })
    expect(need(broken)).toEqual({ cell: 'Halt', state: 'alarm' })
  })
})

describe('most severe account', () => {
  const caution = account({ account: 'LPR 1123', halted: true, halt_kind: 'safety' })
  const halt = account({ account: 'LDA 0042', halted: true, halt_reason: 'strategy error' })
  const book = account({ account: 'LFE 0911', halted: true, halt_kind: 'reconcile' })
  const breach = account({ account: 'LFE 0300', halted: true, halt_kind: 'breach' })

  it('is none when no account needs anything', () => {
    expect(mostSevere([account(), account({ account: 'B' })])).toBeNull()
    expect(alarmName([])).toBeNull()
  })

  it('ranks alarm over caution, then Book over Risk over Halt', () => {
    expect(mostSevere([caution, halt])?.account.account).toBe('LDA 0042')
    expect(mostSevere([caution, halt, breach, book])?.need).toEqual({ cell: 'Book', state: 'alarm' })
    expect(mostSevere([halt, breach])?.account.account).toBe('LFE 0300')
  })

  it('puts real money first: live, then sim, then dry run', () => {
    const dryBreach = { ...breach, account: 'DRY', mode: 'dry' as const }
    const simHalt = { ...halt, account: 'SIM', mode: 'sim' as const }
    expect(mostSevere([dryBreach, simHalt])?.account.account).toBe('SIM')
    expect(mostSevere([simHalt, { ...halt, account: 'LIVE' }])?.account.account).toBe('LIVE')
  })

  it('keeps the server order on a tie', () => {
    expect(mostSevere([{ ...book, account: 'first' }, book])?.account.account).toBe('first')
  })

  it('names only an account in alarm beside the strip', () => {
    expect(alarmName([caution, book])).toBe('Book · LFE 0911')
    expect(alarmName([halt])).toBe('Halt · LDA 0042')
    expect(alarmName([caution])).toBeNull()
  })
})

describe('clock and ages', () => {
  it('reads New York time, 24-hour, across daylight saving', () => {
    expect(nyTime(new Date('2026-10-13T18:31:07Z'))).toBe('14:31:07')
    expect(nyTime(new Date('2026-12-01T05:00:00Z'))).toBe('00:00:00')
    expect(nyTime(Date.parse('soon'))).toBe('—')
  })

  it('says how long ago, compactly; null for no or an unreadable time', () => {
    const now = Date.parse('2026-10-13T18:31:07Z')
    expect(age('2026-10-13T18:31:05.250Z', now)).toBe('1s')
    expect(age('2026-10-13T18:28:00+00:00', now)).toBe('3m')
    expect(age('2026-10-13T15:00:00+00:00', now)).toBe('3h')
    expect(age('2026-10-10T18:00:00+00:00', now)).toBe('3d')
    expect(age('2026-10-13T18:31:09Z', now)).toBe('0s') // a clock ahead of ours
    expect(age(null, now)).toBeNull()
    expect(age('soon', now)).toBeNull()
  })
})

describe('kill switch', () => {
  it('counts armed accounts and open positions', () => {
    const long = account({ position: { qty: 2, entry: 1, stop: 0, target: 2 } })
    const short = account({ position: { qty: -1, entry: 1, stop: 2, target: 0 } })
    const broken = account({ position: null })
    expect(killCounts([long, short, broken, account()])).toEqual(['4 accounts armed', '2 open positions'])
    expect(killCounts([long])).toEqual(['1 account armed', '1 open position'])
    expect(killCounts([])).toEqual(['0 accounts armed', 'No open position'])
  })

  it('counts dry-run positions apart, as paper: the flatten never touches them', () => {
    const paper = account({ mode: 'dry', position: { qty: 1, entry: 1, stop: 0, target: 2 } })
    const real = account({ mode: 'sim', position: { qty: -2, entry: 1, stop: 2, target: 0 } })
    expect(killCounts([paper])).toEqual(['1 account armed', 'No open position · 1 paper (dry run)'])
    expect(killCounts([real, paper, paper])).toEqual(['3 accounts armed', '1 open position · 2 paper (dry run)'])
  })

  it('reads a failure as such, warning that part of it may have run', () => {
    expect(killText({ ok: true, error: null })).toMatch(/^Kill switch ran/)
    const text = killText({ ok: false, error: 'flatten all failed: ConnectError' })
    expect(text).toMatch(/^Kill switch failed: flatten all failed: ConnectError\./)
    expect(text).toContain('may already be disarmed or halted')
    // the server's own full stop is not doubled
    expect(killText({ ok: false, error: 'disarm failed: database is locked.' })).toMatch(/^Kill switch failed: disarm failed: database is locked\. Accounts/)
  })

  it('reads a failed request as "may not have run", a timeout as no answer', () => {
    expect(killErrorText(new ApiError(502, 'Bad Gateway'))).toBe(
      'Kill switch request failed (502: Bad Gateway). It may not have run: check NinjaTrader and press it again.',
    )
    expect(killErrorText(new TypeError('Failed to fetch'))).toMatch(/^Kill switch request failed \(Failed to fetch\)\. It may not have run/)
    expect(killErrorText(new DOMException('signal timed out', 'TimeoutError'))).toBe(
      'Kill switch got no answer in 90 s. It may not have run: check NinjaTrader and press it again.',
    )
    expect(KILL_TIMEOUT_MS).toBe(90_000)
  })
})

describe('account strip', () => {
  it('is Risk · Book: Risk caution on a safety halt, alarm on a breach; Book alarm on a mismatch', () => {
    const states = (a: Parameters<typeof accountStrip>[0]) => accountStrip(a).map((c) => `${c.name} ${c.state}`)
    expect(states(account())).toEqual(['Risk normal', 'Book normal'])
    expect(states(account({ halted: true, halt_kind: 'safety' }))).toEqual(['Risk caution', 'Book normal'])
    expect(states(account({ halted: true, halt_kind: 'breach' }))).toEqual(['Risk alarm', 'Book normal'])
    expect(states(account({ halted: true, halt_kind: 'reconcile' }))).toEqual(['Risk normal', 'Book alarm'])
  })

  it('stays normal for stale and drift pauses (the system strip carries them)', () => {
    for (const kind of ['stale', 'drift'] as const)
      expect(accountStrip(account({ halted: true, halt_kind: kind })).map((c) => c.state)).toEqual(['normal', 'normal'])
  })
})

describe('focus', () => {
  const accounts = desk().accounts
  const id = (f: ReturnType<typeof focusOf>) => f && `${f.account.account}${f.picked ? ' (picked)' : ''}`

  it('focuses the most severe account: the book mismatch (alarm) over the safety halt (caution)', () => {
    expect(id(focusOf(accounts, null))).toBe('LFE 0911')
  })

  it("keeps the user's pick while it is armed and no new alarm came, and falls back when it is gone", () => {
    expect(id(focusOf(accounts, pickOf('LDA 0042', accounts)))).toBe('LDA 0042 (picked)')
    expect(id(focusOf(accounts, { account: 'LFE 9999', alarms: ['LFE 0911'] }))).toBe('LFE 0911')
  })

  it('a new alarm takes focus from the pick; the alarms that stood at the pick do not', () => {
    const calm = accounts.filter((a) => a.account !== 'LFE 0911')
    const pick = pickOf('LDA 0042', calm) // nothing in alarm then
    expect(pick.alarms).toEqual([])
    expect(id(focusOf(calm, pick))).toBe('LDA 0042 (picked)')
    expect(id(focusOf(accounts, pick))).toBe('LFE 0911') // LFE 0911 went into alarm since
    expect(focusOf(accounts, pick)?.takeover).toBe(true)
    expect(id(focusOf(accounts, pickOf('LDA 0042', accounts)))).toBe('LDA 0042 (picked)')
    expect(alarmsOf(accounts)).toEqual(['LFE 0911'])
  })

  it('hands focus to the new alarm, never back to an alarm already seen (even a more severe one)', () => {
    const a = account({ account: 'A', mode: 'sim' })
    const b = account({ account: 'B', mode: 'live', halted: true, halt_kind: 'reconcile' }) // live outranks sim
    const pick = pickOf('A', [a, b]) // B was in alarm when the user picked A
    expect(pick.alarms).toEqual(['B'])
    expect(id(focusOf([a, b], pick))).toBe('A (picked)')
    const aAlarm = { ...a, ...held({ kind: 'reconcile', reason: 'mismatch' }) }
    expect(focusOf([aAlarm, b], pick)).toMatchObject({ account: { account: 'A' }, takeover: true })
    // a new alarm on a third, sim account while B (live) is in alarm: focus the new one
    const c = account({ account: 'C', mode: 'sim', halted: true, halt_kind: 'breach' })
    expect(focusOf([a, b, c], pick)?.account.account).toBe('C')
    // the most severe of several new alarms
    const d = account({ account: 'D', mode: 'live', halted: true, halt_kind: 'breach' })
    expect(focusOf([a, b, c, d], pick)?.account.account).toBe('D')
    // recorded as the new pick, it holds until a further alarm
    expect(id(focusOf([a, b, c], pickOf('C', [a, b, c])))).toBe('C (picked)')
  })

  it('with nothing wrong, focuses real money first: live, then sim, then dry run', () => {
    const calm = [account({ account: 'D', mode: 'dry' }), account({ account: 'S', mode: 'sim' }), account({ account: 'L' })]
    expect(id(focusOf(calm, null))).toBe('L')
    expect(id(focusOf(calm.slice(0, 2), null))).toBe('S')
    expect(focusOf([], null)).toBeNull()
  })
})

describe('resolve', () => {
  it('shows for reconcile and drift pauses only, never for a halt or breach', () => {
    expect(canResolve(account({ halted: true, halt_kind: 'reconcile' }))).toBe(true)
    expect(canResolve(account({ halted: true, halt_kind: 'drift' }))).toBe(true)
    for (const kind of ['safety', 'breach', 'stale', null] as const) expect(canResolve(account({ halted: true, halt_kind: kind }))).toBe(false)
  })

  it('says what came back: ran, could not read the book, not running (404), server error (500), no answer', () => {
    expect(resolveText('LFE 0911', { ok: true }, null)).toEqual({
      tone: 'normal',
      text: 'Resolve ran for LFE 0911: pauses lifted, NinjaTrader re-checked. A mismatch still there pauses it again.',
    })
    expect(resolveText('LFE 0911', { ok: false }, null).tone).toBe('caution')
    const missing = resolveText('LFE 0911', undefined, new ApiError(404, '{"detail":"\'LFE 0911\' is not running"}'))
    expect(missing).toEqual({ tone: 'caution', text: 'LFE 0911 is not running: nothing to resolve.' })
    expect(missing.text).not.toContain('armed')
    const failed = resolveText('LFE 0911', undefined, new ApiError(500, 'Internal Server Error'))
    expect(failed.tone).toBe('alarm')
    expect(failed.text).toContain('the pause may already be lifted')
    expect(resolveText('LFE 0911', undefined, new DOMException('timed out', 'TimeoutError'))).toEqual({
      tone: 'alarm',
      text: 'Resolve for LFE 0911 got no answer in 90 s: it may still run. The status shows what holds.',
    })
    expect(resolveText('LFE 0911', undefined, new TypeError('Failed to fetch')).text).toBe(
      'Resolve request for LFE 0911 failed (Failed to fetch): it may not have run.',
    )
  })
})

describe('alert tone', () => {
  it('is the need, caution for a stale or drift pause, null when all is well', () => {
    expect(alertTone(account({ halted: true, halt_kind: 'reconcile' }))).toBe('alarm')
    expect(alertTone(account({ halted: true, halt_kind: 'safety' }))).toBe('caution')
    expect(alertTone(account({ halted: true, halt_kind: 'drift' }))).toBe('caution')
    expect(alertTone(account())).toBeNull()
  })
})

describe('figures', () => {
  it('dollars with cents only when there are any, P&L signed', () => {
    expect(usd(2000)).toBe('$2,000')
    expect(usd(1412.5)).toBe('$1,412.50')
    expect(usd(-588)).toBe('−$588')
    expect(usd(212, true)).toBe('+$212')
    expect(usd(-812.25, true)).toBe('−$812.25')
    expect(usd(0, true)).toBe('$0')
  })

  it('position and bracket', () => {
    expect(positionText({ qty: 2, entry: 25412.25, stop: 25402.25, target: 25432.25 })).toBe('Long 2 @ 25,412.25')
    expect(positionText({ qty: -1, entry: null, stop: null, target: null })).toBe('Short 1')
    expect(positionText({ qty: 0, entry: null, stop: null, target: null })).toBe('Flat')
    expect(positionText(null)).toBe('—')
    expect(bracketText({ qty: 2, entry: 1, stop: 25402.25, target: 25432.25 })).toBe('Stop 25,402.25 · Target 25,432.25')
    expect(bracketText({ qty: 2, entry: 1, stop: null, target: null })).toBe('No stop known')
    expect(bracketText({ qty: 0, entry: null, stop: null, target: null })).toBe('—')
  })

  it('account type in words', () => {
    expect(typeText(account({ rules_account: 'lucid-flex-50k' }))).toBe('lucid flex 50k')
  })
})

describe('meters', () => {
  const [, safety, mismatch] = desk().accounts as [unknown, LiveStatus['accounts'][number], LiveStatus['accounts'][number]]

  it('drawdown, daily loss, contracts and, in evaluation, the target', () => {
    expect(meters(mismatch).map((m) => m.name)).toEqual(['Drawdown left', 'Daily loss left', 'Contracts', 'Target'])
    expect(meters(mismatch)[1]).toEqual({ name: 'Daily loss left', none: 'No limit' })
    // funded: no target
    expect(meters(safety).map((m) => m.name)).toEqual(['Drawdown left', 'Daily loss left', 'Contracts'])
    expect(meters(account({ limits: null }))).toEqual([])
  })

  it('marks the safety level: today’s loss at the safety equity, and the drawdown left there', () => {
    const daily = meters(safety)[1] as Exclude<Meter, { none: string }>
    expect(daily).toMatchObject({ figure: '$188', of: 'of $1,000', used: 812, max: 1000, alarm: 'full' })
    expect(daily.safety).toBeCloseTo(0.8) // (start 172 − safety −628) / 1000
    const dd = meters(mismatch)[0] as Exclude<Meter, { none: string }>
    expect(dd.safety).toBeCloseTo(0.8) // 1 − (−2076 − −2676) / 3000
  })

  it('lights cells Steel, Ember from the safety mark on, Red at 100% of a loss limit only', () => {
    const m = { name: 'Daily loss left', figure: '', of: '', used: 812, max: 1000, safety: 0.8, alarm: 'full' as const }
    const cells = segments(m)
    expect(cells).toHaveLength(20)
    expect(cells.filter((c) => c.state === 'caution')).toHaveLength(16)
    expect(cells.findIndex((c) => c.mark)).toBe(15)
    expect(segments({ ...m, used: 500 }).filter((c) => c.state === 'on')).toHaveLength(10)
    expect(segments({ ...m, used: 1200 }).every((c) => c.state === 'alarm')).toBe(true)
    // exactly at the safety fraction the server halts (at or below the level): Ember already
    expect(segments({ ...m, used: 800 }).filter((c) => c.state === 'caution')).toHaveLength(16)
    expect(segments({ ...m, used: 790 }).filter((c) => c.state === 'caution')).toHaveLength(0)
    expect(segments({ ...m, used: 1000, alarm: null, safety: null }).every((c) => c.state === 'on')).toBe(true)
    expect(segments({ ...m, safety: null }).some((c) => c.mark)).toBe(false)
  })

  it('contracts: minis rounded to 0.1, Red only past the cap', () => {
    const three = meters(account({ limits: { drawdown_remaining: 1, drawdown_max: 1, daily_loss_remaining: null, daily_loss_max: null, max_minis: 4, held_minis: 3 * 0.1 } }))[2]
    expect(three).toMatchObject({ name: 'Contracts', figure: '0.3', of: 'of 4 minis', alarm: 'over' })
    expect(minis(0.6000000000000001)).toBe('0.6')
    expect(minis(2)).toBe('2')
    const c = { name: 'Contracts', figure: '', of: '', used: 4, max: 4, safety: null, alarm: 'over' as const }
    expect(segments(c).every((x) => x.state === 'on')).toBe(true) // holding the cap is allowed
    expect(segments({ ...c, used: 4.1 }).every((x) => x.state === 'alarm')).toBe(true)
  })

  it('says each meter in words', () => {
    expect(meterLabel(meters(safety)[1]!)).toBe('Daily loss left: 81% used, safety mark at 80%')
    expect(meterLabel({ name: 'Daily loss left', none: 'No limit' })).toBe('Daily loss left: no limit')
    expect(meterLabel(meters(mismatch)[3]!)).toBe('Target: 7% of target')
  })
})

describe('each condition counts, whatever the halt (#212)', () => {
  const safety = { kind: 'safety', reason: 'safety margin: 81% of the daily loss used' } as const
  const mismatch = { kind: 'reconcile', reason: 'NinjaTrader mismatch: stray_orders: o1' } as const
  const stale = { kind: 'stale', reason: 'no NQ bar for 191s; new entries paused' } as const
  const strip = (a: Parameters<typeof accountStrip>[0]) => accountStrip(a).map((c) => `${c.name} ${c.state}`)

  it('a reconcile mismatch on a safety-halted account is a Book alarm', () => {
    const a = account({ account: 'LPR 1123', mode: 'sim', ...held(safety, mismatch) })
    expect(a.halt_kind).toBe('safety') // the server shows the halt first
    expect(need(a)).toEqual({ cell: 'Book', state: 'alarm' })
    expect(strip(a)).toEqual(['Risk caution', 'Book alarm'])
    expect(alarmName([account({ account: 'LFE 0507', ...held(safety) }), a])).toBe('Book · LPR 1123')
    expect(alarmsOf([a])).toEqual(['LPR 1123'])
    expect(canResolve(a)).toBe(true) // Resolve lifts the pause, never the halt
    expect(alertTone(a)).toBe('alarm')
  })

  it('a breach and a mismatch light both cells', () => {
    expect(strip(account(held({ kind: 'breach', reason: 'breach max_drawdown: x' }, mismatch)))).toEqual(['Risk alarm', 'Book alarm'])
    expect(strip(account(held({ kind: null, reason: 'strategy error: x' }, mismatch)))).toEqual(['Risk normal', 'Book alarm'])
    expect(need(account(held({ kind: null, reason: 'strategy error: x' }, safety)))).toEqual({ cell: 'Halt', state: 'alarm' })
  })

  it('a stale pause lights Feed whatever halt the account is under', () => {
    for (const halt of [safety, { kind: 'breach', reason: 'breach' } as const, { kind: null, reason: 'strategy error' } as const, mismatch])
      expect(states(status({ accounts: [account(held(halt, stale))] }))[0]).toBe('caution')
    expect(states(status({ accounts: [account(held(safety))] }))[0]).toBe('normal')
  })

  it('a drift pause under a halt still offers Resolve', () => {
    expect(canResolve(account(held(safety, { kind: 'drift', reason: 'drift: 9 ticks' })))).toBe(true)
    expect(canResolve(account(held(safety, stale)))).toBe(false)
  })

  it('the focus lists every reason, each in its own tone; the brief unreconciled pause as plain text', () => {
    expect(reasons(account(held(safety, mismatch, stale)))).toEqual([
      { tone: 'caution', text: safety.reason },
      { tone: 'alarm', text: mismatch.reason },
      { tone: 'caution', text: stale.reason },
    ])
    expect(reasons(account(held({ kind: null, reason: 'cannot start: boom' })))).toEqual([{ tone: 'alarm', text: 'cannot start: boom' }])
    expect(reasons(account({ halt_reason: 'not reconciled with NinjaTrader yet: no new entries' }))).toEqual([
      { tone: 'normal', text: 'not reconciled with NinjaTrader yet: no new entries' },
    ])
    expect(reasons(account())).toEqual([])
  })

  it("NinjaTrader's balance at or below 0 shows in the focus in caution, holding nothing back and lighting no cell (#220)", () => {
    const figures = { kind: 'figures', reason: 'NinjaTrader reports a balance at or below 0, which is not a figure' } as const
    const a = account(held(figures))
    expect(a.halted).toBe(false)
    expect(reasons(a)).toEqual([{ tone: 'caution', text: figures.reason }])
    expect(need(a)).toBeNull()
    expect(strip(a)).toEqual(['Risk normal', 'Book normal'])
    expect(states(status({ accounts: [a] }))).toEqual(['normal', 'normal', 'normal'])
    expect(reasons(account(held(safety, figures)))).toEqual([
      { tone: 'caution', text: safety.reason },
      { tone: 'caution', text: figures.reason },
    ])
  })
})

describe('warming up (#79): a condition, not a halt', () => {
  const warming = { kind: 'warming', reason: "warming up: today's NQ bars from LSE are not complete yet" } as const
  const stale = { kind: 'stale', reason: 'no NQ bar for 191s; new entries paused' } as const
  const a = account({ halt_reason: warming.reason, conditions: [warming] }) // halted stays false

  it('lights Feed caution, under a stale pause or a halt too', () => {
    expect(states(status({ accounts: [a] }))[0]).toBe('caution')
    const safety = { kind: 'safety', reason: 'safety margin' } as const
    expect(states(status({ accounts: [account({ ...held(safety, stale, warming) })] }))[0]).toBe('caution')
    expect(states(status({ lse: lseDown, accounts: [a] }))[0]).toBe('alarm')
  })

  it('shows its reason in caution, after a stale pause, needs no cell, and outlines the account in caution', () => {
    expect(reasons(a)).toEqual([{ tone: 'caution', text: warming.reason }])
    expect(reasons(account(held(stale, warming))).map((r) => r.text)).toEqual([stale.reason, warming.reason])
    expect(need(a)).toBeNull()
    expect(alertTone(a)).toBe('caution')
  })
})

describe('feed outage', () => {
  const now = Date.parse('2026-10-13T18:31:00Z')
  const lse = (error: string | null, retry_at: string | null) => ({ connected: error === null, last_bar_ts: null, error, retry_at })

  it('is nothing while the feed has no error', () => {
    expect(feedDown(lse(null, null), now)).toBeNull()
  })

  it('names the reason and the next retry in New York time', () => {
    expect(feedDown(lse('LSE stream stopped: Max 1 concurrent', '2026-10-13T18:33:00+00:00'), now)).toEqual({
      reason: 'LSE stream stopped: Max 1 concurrent',
      retry: 'Retry 14:33:00',
    })
  })

  it('says a restart is needed when no retry is coming, and a passed retry is under way', () => {
    expect(feedDown(lse('FENRIR_LSE_API_KEY is not set', null), now)?.retry).toBe('Restart needed')
    expect(feedDown(lse('refused', '2026-10-13T18:30:00+00:00'), now)?.retry).toBe('Retrying')
  })
})
