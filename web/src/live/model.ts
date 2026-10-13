// Pure state for the Live page: the `GET /api/live/status` shape, the code-strip rules and the
// most severe account (docs/contracts.md "Code strips", DESIGN.md "Code Strip").
import type { Tone } from '@/components/state-text'

export type Mode = 'dry' | 'sim' | 'live'
export type HaltKind = 'safety' | 'reconcile' | 'stale' | 'drift' | 'breach'

type Limits = {
  drawdown_remaining: number
  drawdown_max: number
  daily_loss_remaining: number | null
  daily_loss_max: number | null
  max_minis: number
  held_minis: number
}
type Position = { qty: number; entry: number | null; stop: number | null; target: number | null }
type Session = {
  start: number
  floor: number
  target: number | null
  safety: number
  equity: [string, number][]
  fills: string[]
}

/** One armed account. An account that cannot start has every figure null. */
export type AccountStatus = {
  account: string
  mode: Mode
  phase: 'evaluation' | 'funded'
  strategy: string
  rules_account: string
  contract: string
  halted: boolean
  halt_kind: HaltKind | null
  halt_reason: string | null
  balance: number | null
  nt_balance: number | null
  today_pnl: number | null
  limits: Limits | null
  position: Position | null
  session: Session | null
}

export type LiveStatus = {
  lse: { connected: boolean; last_bar_ts: string | null }
  nt: { connected: boolean; last_heartbeat: string | null }
  drift: { ok: boolean; ticks: number | null }
  accounts: AccountStatus[]
}

/** `POST /api/live/kill`: always 200 once it ran; `ok: false` names what failed. */
export type KillResult = { ok: boolean; error: string | null }

export type Cell = { name: string; state: Tone }

/** Feed · Broker · Drift, shared by every account. Stale and drift pauses light these, never an account. */
export function systemStrip(s: LiveStatus): Cell[] {
  const kinds = s.accounts.map((a) => a.halt_kind)
  const feed: Tone = !s.lse.connected ? 'alarm' : kinds.includes('stale') ? 'caution' : 'normal'
  // A missed heartbeat reaches us as nt.connected false. Dry runs never use the broker.
  const broker: Tone = s.accounts.every((a) => a.mode === 'dry') ? 'idle' : s.nt.connected ? 'normal' : 'alarm'
  const drift: Tone = s.drift.ok ? 'normal' : 'caution'
  return [
    { name: 'Feed', state: feed },
    { name: 'Broker', state: broker },
    { name: 'Drift', state: drift },
  ]
}

/** A code strip's `aria-label`: each cell's state in words. */
export const stripLabel = (cells: Cell[]): string =>
  cells.map((c, i) => `${i ? c.name.toLowerCase() : c.name} ${c.state}`).join(', ')

/** What an account needs, as the cell it lights (Risk, Book) or "Halt" for a halt no cell names. */
export type Need = { cell: 'Book' | 'Risk' | 'Halt'; state: 'caution' | 'alarm' }

export function need(a: AccountStatus): Need | null {
  if (a.halt_kind === 'reconcile') return { cell: 'Book', state: 'alarm' }
  if (a.halt_kind === 'breach') return { cell: 'Risk', state: 'alarm' }
  if (a.halted && a.halt_kind === null) return { cell: 'Halt', state: 'alarm' } // strategy error, cannot start, …
  const s = a.session
  const pastSafety = s !== null && a.today_pnl !== null && s.start + a.today_pnl <= s.safety
  if (a.halt_kind === 'safety' || pastSafety) return { cell: 'Risk', state: 'caution' }
  return null
}

const stateRank = { alarm: 0, caution: 1 } as const
const modeRank: Record<Mode, number> = { live: 0, sim: 1, dry: 2 }
const cellRank = { Book: 0, Risk: 1, Halt: 2 } as const

/** The account most in need: alarm before caution, then real money first (live, sim, dry run), then
 * Book (a position mismatch) before Risk before Halt; ties keep the server's order. Null when none needs anything. */
export function mostSevere(accounts: AccountStatus[]): { account: AccountStatus; need: Need } | null {
  const needing = accounts.flatMap((account) => {
    const n = need(account)
    return n ? [{ account, need: n }] : []
  })
  // sort is stable, so ties keep the server's order
  needing.sort(
    (x, y) =>
      stateRank[x.need.state] - stateRank[y.need.state] ||
      modeRank[x.account.mode] - modeRank[y.account.mode] ||
      cellRank[x.need.cell] - cellRank[y.need.cell],
  )
  return needing[0] ?? null
}

/** The name beside the system strip: the most severe account in alarm, e.g. "Book · LFE 0911". */
export function alarmName(accounts: AccountStatus[]): string | null {
  const m = mostSevere(accounts)
  return m && m.need.state === 'alarm' ? `${m.need.cell} · ${m.account.account}` : null
}

const nyFormat = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

/** HH:MM:SS in New York, the session's clock; a dash for an unreadable time, never a throw (the page holds the kill switch). */
export const nyTime = (t: Date | number): string => (Number.isNaN(+t) ? '—' : nyFormat.format(t))

/** How long ago `ts` was, compactly ("4s", "3m", "2h", "1d"); null for no or an unreadable time. */
export function age(ts: string | null, now: number): string | null {
  const t = ts === null ? NaN : Date.parse(ts)
  if (Number.isNaN(t)) return null
  const s = Math.max(0, Math.floor((now - t) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

/** The kill dialog's counts: armed accounts and the positions their runners hold. */
export function killCounts(accounts: AccountStatus[]): string[] {
  const open = accounts.filter((a) => (a.position?.qty ?? 0) !== 0).length
  const n = accounts.length
  return [
    `${n} ${n === 1 ? 'account' : 'accounts'} armed`,
    open === 0 ? 'No open position' : `${open} open ${open === 1 ? 'position' : 'positions'}`,
  ]
}

/** The kill switch's answer as text. `ok: false` is an alarm: some of it may have run already. */
export const killText = (r: KillResult): string =>
  r.ok
    ? 'Kill switch ran: flattened and cancelled all, every account disarmed.'
    : `Kill switch failed: ${r.error ?? 'no details'}. Accounts may already be disarmed or halted; check NinjaTrader for open positions and working orders.`
