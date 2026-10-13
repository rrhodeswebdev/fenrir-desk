import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { account, status } from './fixtures'
import {
  type LiveStatus,
  KILL_TIMEOUT_MS,
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

describe('system strip', () => {
  it('is normal when the feed and the broker are up and nothing drifted', () => {
    expect(systemStrip(status())).toEqual([
      { name: 'Feed', state: 'normal' },
      { name: 'Broker', state: 'normal' },
      { name: 'Drift', state: 'normal' },
    ])
  })

  it('Feed: alarm when LSE is not connected, caution when an account has a stale pause', () => {
    expect(states(status({ lse: { connected: false, last_bar_ts: null } }))[0]).toBe('alarm')
    const stale = account({ halted: true, halt_kind: 'stale', halt_reason: 'no bar for 90 s' })
    expect(states(status({ accounts: [account(), stale] }))[0]).toBe('caution')
    // a lost feed outranks the pause it causes
    expect(states(status({ lse: { connected: false, last_bar_ts: null }, accounts: [stale] }))[0]).toBe('alarm')
    // a dry run steps on live bars too
    expect(states(status({ lse: { connected: false, last_bar_ts: null }, accounts: [account({ mode: 'dry' })] }))[0]).toBe('alarm')
  })

  it('Feed: idle with nothing armed, as nothing reads it', () => {
    expect(states(status({ lse: { connected: false, last_bar_ts: null }, accounts: [] }))[0]).toBe('idle')
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
