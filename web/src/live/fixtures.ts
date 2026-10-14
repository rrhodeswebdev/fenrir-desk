// Status fixtures for the Live page's tests (Vitest and Playwright). Never imported by the app.
import type { JournalRow } from './journal'
import type { AccountStatus, Condition, LiveStatus } from './model'

/** An account; unless `conditions` is given, a halted one has the one condition its `halt_kind` and `halt_reason` name,
 * as the server reports a single condition. */
export function account(over: Partial<AccountStatus> = {}): AccountStatus {
  return {
    account: 'LFE 0507',
    mode: 'live',
    phase: 'evaluation',
    strategy: 'orb',
    rules_account: 'lucid-flex-50k',
    contract: 'MNQZ6',
    halted: false,
    halt_kind: null,
    halt_reason: null,
    balance: 0,
    nt_balance: 50_000,
    today_pnl: 0,
    limits: { drawdown_remaining: 2000, drawdown_max: 2000, daily_loss_remaining: null, daily_loss_max: null, max_minis: 4, held_minis: 0 },
    position: { qty: 0, entry: null, stop: null, target: null },
    session: { start: 0, floor: -2000, target: 3000, safety: -400, equity: [], fills: [] },
    conditions: over.halted ? [{ kind: over.halt_kind ?? null, reason: over.halt_reason ?? '' }] : [],
    ...over,
  }
}

/** The status fields of an account under `conditions`: the first shows as `halt_kind`/`halt_reason`, as the server does. */
export const held = (...conditions: Condition[]): Pick<AccountStatus, 'halted' | 'halt_kind' | 'halt_reason' | 'conditions'> => ({
  halted: conditions.length > 0,
  halt_kind: conditions[0]?.kind ?? null,
  halt_reason: conditions[0]?.reason ?? null,
  conditions,
})

export function status(over: Partial<LiveStatus> = {}): LiveStatus {
  return {
    lse: { connected: true, last_bar_ts: '2026-10-13T18:31:00+00:00', error: null, retry_at: null },
    nt: { connected: true, last_heartbeat: '2026-10-13T18:31:05.250Z' },
    drift: { ok: true, ticks: null },
    accounts: [account()],
    ...over,
  }
}

/** One equity point per 5 minutes of trading day 2026-10-13 from 18:00 New York to 14:30: flat overnight, then a
 * deterministic walk from the 09:30 open that lands on `start + today`. */
export function equity(start: number, today: number, swing = 300): [string, number][] {
  const open = Date.UTC(2026, 9, 12, 22) // 18:00 New York (EDT)
  const pts: [string, number][] = []
  for (let m = 0; m <= 20 * 60 + 30; m += 5) {
    const k = m < 930 ? 0 : (m - 930) / (20 * 60 + 30 - 930)
    const e = start + today * k + swing * Math.sin(k * 9) * Math.sin(Math.PI * k)
    pts.push([new Date(open + m * 60_000).toISOString().replace('Z', '+00:00'), Math.round(e * 100) / 100])
  }
  return pts
}

/** The Live page's scene, with figures as the server's `view()` gives them (drawdown left = equity − floor; the floor
 * trails 2,000 or 3,000 below the end-of-day peak; safety = floor + 20% of open − floor, or the DLL level when higher): live with a position, sim on a safety halt, sim with a book mismatch, a dry run. */
export function desk(): LiveStatus {
  return status({
    accounts: [
      account({
        today_pnl: 212,
        balance: 1180,
        nt_balance: 51_180,
        limits: { drawdown_remaining: 1768, drawdown_max: 2000, daily_loss_remaining: null, daily_loss_max: null, max_minis: 4, held_minis: 0.2 },
        position: { qty: 2, entry: 25412.25, stop: 25402.25, target: 25432.25 },
        session: { start: 968, floor: -588, target: 3000, safety: -276.8, equity: equity(968, 212), fills: ['2026-10-13T14:41:00+00:00', '2026-10-13T18:12:00+00:00'] },
      }),
      account({
        account: 'LPR 1123',
        mode: 'sim',
        phase: 'funded',
        strategy: 'vwap_fade',
        rules_account: 'lucid-pro-50k',
        contract: 'NQZ6',
        halted: true,
        halt_kind: 'safety',
        halt_reason: 'safety margin: 81% of the daily loss used',
        today_pnl: -812,
        balance: -640,
        nt_balance: 49_360,
        limits: { drawdown_remaining: 1860, drawdown_max: 2500, daily_loss_remaining: 188, daily_loss_max: 1000, max_minis: 4, held_minis: 0 },
        session: { start: 172, floor: -2500, target: null, safety: -628, equity: equity(172, -812, 200), fills: ['2026-10-13T17:58:41+00:00'] },
      }),
      account({
        account: 'LFE 0911',
        mode: 'sim',
        rules_account: 'lucid-flex-100k',
        halted: true,
        halt_kind: 'reconcile',
        halt_reason: 'position mismatch: NinjaTrader long 1, runner flat',
        today_pnl: 96,
        balance: 420,
        nt_balance: 100_420,
        limits: { drawdown_remaining: 3096, drawdown_max: 3000, daily_loss_remaining: null, daily_loss_max: null, max_minis: 6, held_minis: 0 },
        session: { start: 324, floor: -2676, target: 6000, safety: -2076, equity: equity(324, 96, 150), fills: [] },
      }),
      account({
        account: 'LDA 0042',
        mode: 'dry',
        rules_account: 'lucid-daily-25k',
        contract: 'MESZ6',
        nt_balance: null, // dry runs never read NinjaTrader
        limits: { drawdown_remaining: 1000, drawdown_max: 1000, daily_loss_remaining: 500, daily_loss_max: 500, max_minis: 2, held_minis: 0 },
        session: { start: 0, floor: -1000, target: 1250, safety: -400, equity: equity(0, 0, 0), fills: [] },
      }),
    ],
  })
}

let rowId = 0
export const row = (account: string, ts: string, kind: JournalRow['kind'], payload: Record<string, unknown>): JournalRow => ({
  id: ++rowId,
  ts,
  account,
  kind,
  payload,
})

/** Today's journal for `desk()`, by account, as `GET /api/live/journal` returns it (oldest first). */
export function deskJournal(): Record<string, JournalRow[]> {
  return {
    'LFE 0507': [
      row('LFE 0507', '2026-10-12T22:00:00+00:00', 'day_start', { day: '2026-10-13', balance: 51200, peak: 51412, ts: '2026-10-12T22:00:00+00:00' }),
      row('LFE 0507', '2026-10-13T13:30:00+00:00', 'alert', { message: 'session open' }),
      row('LFE 0507', '2026-10-13T18:11:00+00:00', 'action', { type: 'Bracket', side: 'long', qty: 2, stop: 25402.25, target: 25432.25, mode: 'live', sent: true, client_id: 'c1' }),
      row('LFE 0507', '2026-10-13T18:12:00+00:00', 'fill', { type: 'fill', qty: 2, instrument: 'MNQ 12-26', price: 25412.25 }),
      row('LFE 0507', '2026-10-13T18:31:00+00:00', 'bar', { contract: 'MNQZ26', close: 25418.5, actions: 0 }),
      row('LFE 0507', '2026-10-13T18:31:00+00:00', 'peak', { peak: 51412, ts: '2026-10-13T18:31:00+00:00' }),
    ],
    'LPR 1123': [
      row('LPR 1123', '2026-10-13T17:58:41+00:00', 'fill', { type: 'fill', qty: -1, instrument: 'NQ 12-26', price: 25398 }),
      row('LPR 1123', '2026-10-13T17:58:41+00:00', 'halt', { reason: 'safety margin: 81% of the daily loss used', kind: 'safety', until: '2026-10-13' }),
    ],
    'LFE 0911': [
      row('LFE 0911', '2026-10-13T18:30:12+00:00', 'halt', { reason: 'position mismatch: NinjaTrader long 1, runner flat', kind: 'reconcile', until: null }),
    ],
    'LDA 0042': [
      row('LDA 0042', '2026-10-13T17:22:05+00:00', 'rejection', { action: { type: 'Bracket', side: 'long', qty: 4, stop: 6801, target: 6830 }, reason: 'stop-out would breach the daily loss limit' }),
    ],
  }
}
