// Pure state for the Live page: the `GET /api/live/status` shape, the code-strip rules and the
// most severe account (docs/contracts.md "Code strips", DESIGN.md "Code Strip").
import { ApiError } from '@/api'
import type { Tone } from '@/components/state-text'
import { errorText } from '@/errorText'

export type Mode = 'dry' | 'sim' | 'live'
/** `figures` (#220): a pause while NinjaTrader reports a balance at or below 0, as the stale and drift pauses. */
export type HaltKind = 'safety' | 'reconcile' | 'stale' | 'drift' | 'breach' | 'warming' | 'figures'
/** One condition holding new entries back; `kind` null is a halt no strip cell names (strategy error, cannot start, …).
 * `warming` (#79) is no halt: the account waits for today's bars or the bar store, with `halted` false. */
export type Condition = { kind: HaltKind | null; reason: string }

type Limits = {
  drawdown_remaining: number
  drawdown_max: number
  daily_loss_remaining: number | null
  daily_loss_max: number | null
  max_minis: number
  held_minis: number
}
export type Position = { qty: number; entry: number | null; stop: number | null; target: number | null }
export type Session = {
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
  /** Every condition, each reported even under another (a mismatch under a safety halt); `halt_kind` and
   * `halt_reason` show only the first. The strips read these. */
  conditions: Condition[]
  balance: number | null
  nt_balance: number | null
  today_pnl: number | null
  limits: Limits | null
  position: Position | null
  session: Session | null
}

export type LiveStatus = {
  /** `error`: why the feed is down, until a bar or an authenticated resubscribe; `retry_at` null: only a restart helps. */
  lse: { connected: boolean; last_bar_ts: string | null; error: string | null; retry_at: string | null }
  nt: { connected: boolean; last_heartbeat: string | null }
  drift: { ok: boolean; ticks: number | null }
  accounts: AccountStatus[]
}

/** `POST /api/live/kill`: always 200 once it ran; `ok: false` names what failed. */
export type KillResult = { ok: boolean; error: string | null }

export type Cell = { name: string; state: Tone }

/** Whether the account is under a condition of this kind, whatever else holds it. */
const has = (a: AccountStatus, kind: HaltKind | null): boolean => a.conditions.some((c) => c.kind === kind)

/** Feed · Broker · Drift, shared by every account. Stale, warming-up, figures and drift pauses light these, never an account. */
export function systemStrip(s: LiveStatus): Cell[] {
  const stale = s.accounts.some((a) => has(a, 'stale') || has(a, 'warming')) // under a halt too
  // With nothing armed nothing reads the feed (as Broker): unlit, not an alarm to learn to ignore.
  const feed: Tone = s.accounts.length === 0 ? 'idle' : !s.lse.connected ? 'alarm' : stale ? 'caution' : 'normal'
  // A missed heartbeat reaches us as nt.connected false. Dry runs never use the broker.
  // A balance at or below 0 from NinjaTrader (`figures`, #220) is caution, under a halt too.
  const figures = s.accounts.some((a) => has(a, 'figures'))
  const broker: Tone = s.accounts.every((a) => a.mode === 'dry') ? 'idle' : !s.nt.connected ? 'alarm' : figures ? 'caution' : 'normal'
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

const stateRank = { alarm: 0, caution: 1 } as const
const modeRank: Record<Mode, number> = { live: 0, sim: 1, dry: 2 }
const cellRank = { Book: 0, Risk: 1, Halt: 2 } as const
const byNeed = (x: Need, y: Need) => stateRank[x.state] - stateRank[y.state] || cellRank[x.cell] - cellRank[y.cell]

/** Everything an account needs, one per condition (a mismatch under a safety halt is both), most severe first. */
export function needs(a: AccountStatus): Need[] {
  const s = a.session
  const pastSafety = s !== null && a.today_pnl !== null && s.start + a.today_pnl <= s.safety
  const out: Need[] = []
  if (has(a, 'reconcile')) out.push({ cell: 'Book', state: 'alarm' })
  if (has(a, 'breach')) out.push({ cell: 'Risk', state: 'alarm' })
  if (has(a, null)) out.push({ cell: 'Halt', state: 'alarm' }) // strategy error, cannot start, …
  if (has(a, 'safety') || pastSafety) out.push({ cell: 'Risk', state: 'caution' })
  return out.sort(byNeed)
}

/** The most severe of `needs`: alarm before caution, then Book, Risk, Halt. */
export const need = (a: AccountStatus): Need | null => needs(a)[0] ?? null

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

/** The kill dialog's counts: armed accounts and the positions their runners hold. Dry-run positions are
 * paper (NinjaTrader never had them, the flatten does not touch them), so they are counted apart. */
export function killCounts(accounts: AccountStatus[]): string[] {
  const holding = accounts.filter((a) => (a.position?.qty ?? 0) !== 0)
  const open = holding.filter((a) => a.mode !== 'dry').length
  const paper = holding.length - open
  const n = accounts.length
  const positions = open === 0 ? 'No open position' : `${open} open ${open === 1 ? 'position' : 'positions'}`
  return [`${n} ${n === 1 ? 'account' : 'accounts'} armed`, paper ? `${positions} · ${paper} paper (dry run)` : positions]
}

/** The kill switch's answer as text. `ok: false` is an alarm: some of it may have run already. */
export const killText = (r: KillResult): string =>
  r.ok
    ? 'Kill switch ran: flattened and cancelled all, every account disarmed.'
    : `Kill switch failed: ${(r.error ?? 'no details').replace(/[.\s]+$/, '')}. Accounts may already be disarmed or halted; check NinjaTrader for open positions and working orders.`

/** How long the page waits for the kill's answer: the hub lock (up to about 60 s) plus a hung post (about 25 s). */
export const KILL_TIMEOUT_MS = 90_000

/** The kill request failed (no answer in time, a refused or non-200 request): it may not have run. */
export const killErrorText = (e: unknown): string =>
  e instanceof DOMException && e.name === 'TimeoutError'
    ? `Kill switch got no answer in ${KILL_TIMEOUT_MS / 1000} s. It may not have run: check NinjaTrader and press it again.`
    : `Kill switch request failed (${errorText(e)}). It may not have run: check NinjaTrader and press it again.`

// ---- Focus field, sleeves and meters (#98) ----

/** An account's own strip, Risk · Book (contract "Code strips"), each cell its most severe need: a mismatch under a
 * safety halt lights both. Stale and drift pauses never light it. */
export function accountStrip(a: AccountStatus): Cell[] {
  const all = needs(a)
  const cell = (name: Need['cell']): Cell => ({ name, state: all.find((n) => n.cell === name)?.state ?? 'normal' })
  return [cell('Risk'), cell('Book')]
}

/** The accounts in alarm now. */
export const alarmsOf = (accounts: AccountStatus[]): string[] =>
  accounts.filter((a) => need(a)?.state === 'alarm').map((a) => a.account)

/** A sleeve the user clicked, with the accounts that were in alarm at that moment. */
export type Pick = { account: string; alarms: string[] }
export const pickOf = (account: string, accounts: AccountStatus[]): Pick => ({ account, alarms: alarmsOf(accounts) })

/** The focused account. The user's pick holds while it is armed and no account has gone into alarm since the pick.
 * A new alarm takes focus (its Resolve and the kill switch must be a glance away): the most severe of the alarms that
 * were not there at the pick, never an alarm the user had already seen and moved away from; `takeover` says so, and
 * the page records it as the new pick. Without a pick, the most severe account, else the first by real money (live,
 * sim, dry run; ties keep the server's order). `picked` says the focus is the user's (or a takeover's) pick. */
export function focusOf(
  accounts: AccountStatus[],
  pick: Pick | null,
): { account: AccountStatus; picked: boolean; takeover: boolean } | null {
  const own = pick && accounts.find((a) => a.account === pick.account)
  if (own) {
    const fresh = mostSevere(accounts.filter((a) => need(a)?.state === 'alarm' && !pick.alarms.includes(a.account)))
    return fresh ? { account: fresh.account, picked: false, takeover: true } : { account: own, picked: true, takeover: false }
  }
  const auto = mostSevere(accounts)?.account ?? [...accounts].sort((x, y) => modeRank[x.mode] - modeRank[y.mode])[0]
  return auto ? { account: auto, picked: false, takeover: false } : null
}

/** Resolve lifts the reconcile and drift pauses (never a halt or breach), so it shows for those two only, under a halt too. */
export const canResolve = (a: AccountStatus): boolean => has(a, 'reconcile') || has(a, 'drift')

/** The colour of an account's alert line and sleeve outline: its need, or caution for a stale, drift or warming-up
 * pause (entries are paused; the system strip carries the cell). Null when nothing is wrong. */
export function alertTone(a: AccountStatus): 'caution' | 'alarm' | null {
  return need(a)?.state ?? (a.halted || has(a, 'warming') ? 'caution' : null)
}

const conditionTone: Record<HaltKind, 'caution' | 'alarm'> = {
  reconcile: 'alarm',
  breach: 'alarm',
  safety: 'caution',
  stale: 'caution',
  drift: 'caution',
  warming: 'caution',
  figures: 'caution',
}

/** The focus field's lines: every condition's reason in its own tone (a halt no cell names is an alarm), else the
 * brief unreconciled pause's reason, plain. */
export function reasons(a: AccountStatus): { tone: Tone; text: string }[] {
  if (a.conditions.length) return a.conditions.map((c) => ({ tone: c.kind === null ? 'alarm' : conditionTone[c.kind], text: c.reason }))
  return a.halt_reason ? [{ tone: 'normal', text: a.halt_reason }] : []
}

/** The LSE feed's outage for the top strip: its reason, and when it is retried (`Restart needed` when only a restart
 * helps, `Retrying` once that time has passed). Null while the feed has no error. */
export function feedDown(lse: LiveStatus['lse'], now: number): { reason: string; retry: string } | null {
  if (lse.error === null) return null
  const at = lse.retry_at === null ? NaN : Date.parse(lse.retry_at)
  const retry = lse.retry_at === null ? 'Restart needed' : at <= now ? 'Retrying' : `Retry ${nyTime(at)}`
  return { reason: lse.error, retry }
}

export const modeText: Record<Mode, string> = { dry: 'Dry run', sim: 'Sim', live: 'Live' }
export const phaseText = { evaluation: 'Evaluation', funded: 'Funded' } as const
/** `lucid-flex-50k` → `lucid flex 50k`; shown in Account Name caps. */
export const typeText = (a: AccountStatus): string => a.rules_account.replaceAll('-', ' ')

const dollars = (digits: number) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits })
const whole = dollars(0)
const cents = dollars(2)
/** Dollars, cents only when there are any; `signed` puts + or − on a P&L. */
export function usd(x: number, signed = false): string {
  const s = (Math.round(Math.abs(x) * 100) % 100 === 0 ? whole : cents).format(Math.abs(x))
  return x < 0 && s !== '$0' ? `−${s}` : signed && s !== '$0' ? `+${s}` : s
}
/** A price, e.g. 25,412.25. */
export const price = (x: number): string => x.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 })

export function positionText(p: Position | null): string {
  if (!p) return '—'
  if (p.qty === 0) return 'Flat'
  const side = `${p.qty > 0 ? 'Long' : 'Short'} ${Math.abs(p.qty)}`
  return p.entry === null ? side : `${side} @ ${price(p.entry)}`
}
export function bracketText(p: Position | null): string {
  if (!p || p.qty === 0) return '—'
  const parts = [p.stop !== null && `Stop ${price(p.stop)}`, p.target !== null && `Target ${price(p.target)}`].filter(Boolean)
  return parts.length ? parts.join(' · ') : 'No stop known'
}

/** Equity now, relative to phase start: the session start plus today's (stricter) P&L. */
export const equityNow = (a: AccountStatus): number | null =>
  a.session && a.today_pnl !== null ? a.session.start + a.today_pnl : null

/** One segmented meter. `used`/`max` fill it; `safety` (0..1) marks the safety margin's cell and turns lit
 * cells Ember from it on (the server halts at or below the level); `alarm` turns them Red: `full` at 100% (a loss
 * limit used up), `over` only past the cap (contracts: holding the cap is allowed), null never (target progress). */
export type Meter = {
  name: string
  figure: string
  of: string
  used: number
  max: number
  safety: number | null
  alarm: 'full' | 'over' | null
} | { name: string; none: string }

/** Minis as the server sums them (`qty × 0.1` for a micro drifts in floats: 3 MNQ arrives as 0.30000000000000004). */
export const minis = (x: number): string => String(Math.round(x * 10) / 10)

const frac = (x: number) => (x > 0 && x < 1 ? x : null)

export function meters(a: AccountStatus): Meter[] {
  const l = a.limits
  const s = a.session
  if (!l) return []
  const out: Meter[] = [
    {
      name: 'Drawdown left',
      figure: usd(l.drawdown_remaining),
      of: `of ${usd(l.drawdown_max)}`,
      used: l.drawdown_max - l.drawdown_remaining,
      max: l.drawdown_max,
      // at the safety level the distance to the floor is safety − floor
      safety: s ? frac(1 - (s.safety - s.floor) / l.drawdown_max) : null,
      alarm: 'full',
    },
    l.daily_loss_max === null || l.daily_loss_remaining === null
      ? { name: 'Daily loss left', none: 'No limit' }
      : {
          name: 'Daily loss left',
          figure: usd(l.daily_loss_remaining),
          of: `of ${usd(l.daily_loss_max)}`,
          used: l.daily_loss_max - l.daily_loss_remaining,
          max: l.daily_loss_max,
          // at the safety level today's loss is start − safety
          safety: s ? frac((s.start - s.safety) / l.daily_loss_max) : null,
          alarm: 'full',
        },
    {
      name: 'Contracts',
      figure: minis(l.held_minis),
      of: `of ${minis(l.max_minis)} minis`,
      used: l.held_minis,
      max: l.max_minis,
      safety: null,
      alarm: 'over',
    },
  ]
  const eq = equityNow(a)
  if (s?.target != null && eq !== null)
    out.push({ name: 'Target', figure: usd(eq), of: `of ${usd(s.target)}`, used: Math.max(0, eq), max: s.target, safety: null, alarm: null })
  return out
}

export type Segment = { state: 'idle' | 'on' | 'caution' | 'alarm'; mark: boolean }
export const SEGMENTS = 20

/** The meter's 20 cells: lit Steel, Ember from the safety fraction on, Red per `alarm`; `mark` on the safety cell. */
export function segments(m: Exclude<Meter, { none: string }>): Segment[] {
  const f = m.max > 0 ? Math.min(1, Math.max(0, m.used / m.max)) : 0
  const on = Math.round(f * SEGMENTS)
  const red = (m.alarm === 'full' && f >= 1) || (m.alarm === 'over' && m.used > m.max)
  const markAt = m.safety === null ? -1 : Math.max(0, Math.round(m.safety * SEGMENTS) - 1)
  return Array.from({ length: SEGMENTS }, (_, i) => ({
    state: i >= on ? 'idle' : red ? 'alarm' : m.safety !== null && f >= m.safety ? 'caution' : 'on',
    mark: i === markAt,
  }))
}

/** The meter's `aria-label`. */
export function meterLabel(m: Meter): string {
  if ('none' in m) return `${m.name}: ${m.none.toLowerCase()}`
  const f = m.max > 0 ? Math.round((Math.min(1, Math.max(0, m.used / m.max)) * 100)) : 0
  const safety = m.safety === null ? '' : `, safety mark at ${Math.round(m.safety * 100)}%`
  return `${m.name}: ${f}% ${m.name === 'Target' ? 'of target' : 'used'}${safety}`
}

/** How long the page waits for Resolve: the hub lock (up to about 60 s) plus NinjaTrader's two book reads. */
export const RESOLVE_TIMEOUT_MS = 90_000

/** `POST /api/live/resolve`'s answer, or its request error, as a line: 404 is "not running" (the hub has no runner
 * for it); any other error may have lifted the pause already, so the page re-reads the status either way. */
export function resolveText(account: string, r: { ok: boolean } | undefined, e: unknown): { tone: Tone; text: string } {
  if (r?.ok) return { tone: 'normal', text: `Resolve ran for ${account}: pauses lifted, NinjaTrader re-checked. A mismatch still there pauses it again.` }
  if (r) return { tone: 'caution', text: `Resolve for ${account} could not read NinjaTrader's book: nothing lifted. Check the add-on, then resolve again.` }
  if (e instanceof DOMException && e.name === 'TimeoutError')
    return { tone: 'alarm', text: `Resolve for ${account} got no answer in ${RESOLVE_TIMEOUT_MS / 1000} s: it may still run. The status shows what holds.` }
  if (e instanceof ApiError && e.status === 404) return { tone: 'caution', text: `${account} is not running: nothing to resolve.` }
  if (e instanceof ApiError && e.status === 500)
    return { tone: 'alarm', text: `Resolve for ${account} failed on the server (500): the pause may already be lifted. The status shows what holds.` }
  return { tone: 'alarm', text: `Resolve request for ${account} failed (${errorText(e)}): it may not have run.` }
}
