// Status fixtures for the Live page's tests (Vitest and Playwright). Never imported by the app.
import type { AccountStatus, LiveStatus } from './model'

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
    ...over,
  }
}

export function status(over: Partial<LiveStatus> = {}): LiveStatus {
  return {
    lse: { connected: true, last_bar_ts: '2026-10-13T18:31:00+00:00' },
    nt: { connected: true, last_heartbeat: '2026-10-13T18:31:05.250Z' },
    drift: { ok: true, ticks: null },
    accounts: [account()],
    ...over,
  }
}
