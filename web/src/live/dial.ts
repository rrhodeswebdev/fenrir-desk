// Pure geometry of the session dial (DESIGN.md "Session Dial"): angle is time from 18:00 to 17:00 New York,
// radius is equity between the floor and the target. Coordinates are in a 600x600 viewBox.
import { type HaltKind, type Session, usd } from './model'

export const SIZE = 600
const C = SIZE / 2
export const R_IN = SIZE * 0.12
export const R_OUT = SIZE * 0.46
/** The session, 18:00 to 17:00 New York, in minutes. */
export const SESSION_MIN = 23 * 60
/** 09:30 New York, the open: before it the trace is the dimmed overnight segment. */
export const OPEN_MIN = 15 * 60 + 30
/** 16:40 New York: the runner flattens by min(flat_by, 16:40), and every rules file's flat_by is later. */
export const FLAT_BY_MIN = 22 * 60 + 40

const nyClock = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
  hour: 'numeric',
  minute: 'numeric',
  hourCycle: 'h23',
})

/** New York's offset from UTC at `t`, in minutes (−240 in summer, −300 in winter). */
function nyOffset(t: number): number {
  const p = Object.fromEntries(nyClock.formatToParts(t).map((x) => [x.type, Number(x.value)]))
  return Math.round((Date.UTC(p.year!, p.month! - 1, p.day!, p.hour!, p.minute!) - Math.floor(t / 60_000) * 60_000) / 60_000)
}

const minuteAt = (t: number, offset: number) => (((Math.floor(t / 60_000) + offset - 18 * 60) % 1440) + 1440) % 1440

/** Minutes since 18:00 New York for an ISO time; null when unreadable. */
export function sessionMinute(ts: string): number | null {
  const t = Date.parse(ts)
  return Number.isNaN(t) ? null : minuteAt(t, nyOffset(t))
}

/** `sessionMinute` for every time of one session, reading the zone once: New York never changes its clocks during a
 * session (they change at 02:00 on a Sunday, while the market is shut), so one offset holds for all of them. */
function sessionMinutes(times: string[]): (ts: string) => number | null {
  const first = times.map(Date.parse).find((t) => !Number.isNaN(t))
  const offset = first === undefined ? 0 : nyOffset(first)
  return (ts) => {
    const t = Date.parse(ts)
    return Number.isNaN(t) ? null : minuteAt(t, offset)
  }
}

type Pt = { x: number; y: number }
export type Line = { x1: number; y1: number; x2: number; y2: number }
/** `y` is the label's baseline: just above its ring at the top of the dial, pushed outward when it would touch the
 * label of the ring inside it (the floor's ring is the innermost, so the hub never covers a label). `open` is the
 * equity at today's session open (the status's `session.start`), where the trace and today's P&L begin. */
export type Ring = { name: 'floor' | 'safety' | 'open' | 'target'; r: number; atRisk: boolean; label: string; y: number }
export type DialModel = {
  spokes: Line[]
  hours: (Pt & { text: string })[]
  grid: number[]
  flatBy: Line
  rings: Ring[]
  overnight: string
  live: string
  now: Pt | null
  ticks: Line[]
  hub: number
  pnl: string
}

const LABEL_GAP = 8
const LABEL_LINE = 28
const round = (n: number) => Math.round(n * 10) / 10
const angle = (m: number) => (m / SESSION_MIN) * 2 * Math.PI - Math.PI / 2
const at = (m: number, r: number): Pt => ({ x: round(C + r * Math.cos(angle(m))), y: round(C + r * Math.sin(angle(m))) })
const line = (m: number, r1: number, r2: number): Line => {
  const a = at(m, r1)
  const b = at(m, r2)
  return { x1: a.x, y1: a.y, x2: b.x, y2: b.y }
}
const path = (pts: Pt[]) => pts.map((p, i) => `${i ? 'L' : 'M'}${p.x} ${p.y}`).join(' ')

/** The dial for one session. `todayPnl` is the status's stricter P&L: equity now is `start + todayPnl`. The equity
 * points are the app's own marks, which NinjaTrader's figures can only make worse, so the trace ends at the stricter
 * equity (the hub's figure), dropping to it when the two differ: the dot never sits above what the hub says.
 * A funded account has no target, so the outer ring is as far above the open as the floor is below it. */
export function dial(s: Session, todayPnl: number | null, haltKind: HaltKind | null): DialModel {
  const lo = s.floor
  const hi = Math.max(lo + 1, s.target ?? s.start + (s.start - s.floor))
  const radius = (e: number) => R_IN + ((Math.min(hi, Math.max(lo, e)) - lo) / (hi - lo)) * (R_OUT - R_IN)

  const minute = sessionMinutes([...s.equity.map(([ts]) => ts), ...s.fills])
  const pts = s.equity.flatMap(([ts, e]) => {
    const m = minute(ts)
    return m === null ? [] : [{ m, e }]
  })
  const last = pts.at(-1)
  if (last && todayPnl !== null && s.start + todayPnl !== last.e) pts.push({ m: last.m, e: s.start + todayPnl })
  const xy = pts.map(({ m, e }) => at(m, radius(e)))
  // both segments hold the first point at or after the open, so the two join
  const split = pts.findIndex((p) => p.m >= OPEN_MIN)
  const overnight = split === -1 ? xy : xy.slice(0, split + 1)
  const live = split === -1 ? [] : xy.slice(split)

  const ring = (name: Ring['name'], value: number, atRisk: boolean, label: string): Ring => ({
    name,
    r: radius(value),
    atRisk,
    label: `${label} ${usd(value, true)}`,
    y: C - radius(value) - LABEL_GAP,
  })
  const eq = todayPnl === null ? (pts.at(-1)?.e ?? s.start) : s.start + todayPnl
  const rings: Ring[] = [ring('floor', s.floor, haltKind === 'breach' || eq <= s.floor, 'Floor')]
  if (s.safety > s.floor) rings.push(ring('safety', s.safety, haltKind === 'safety' || eq <= s.safety, 'Safety'))
  rings.push(ring('open', s.start, false, 'Open'))
  if (s.target !== null) rings.push(ring('target', s.target, false, 'Target'))
  // inner to outer, each label at least a line above the one inside it
  const byR = [...rings].sort((x, y) => x.r - y.r)
  byR.forEach((r, i) => {
    const inner = byR[i - 1]
    if (inner) r.y = Math.min(r.y, inner.y - LABEL_LINE)
  })

  const ticks = s.fills.flatMap((ts) => {
    const m = minute(ts)
    const e = m === null ? undefined : (pts.find((p) => p.m >= m) ?? pts.at(-1))?.e
    return m === null || e === undefined ? [] : [line(m, radius(e) - 12, radius(e) + 12)]
  })

  const hours = Array.from({ length: 23 }, (_, h) => h)
  return {
    spokes: hours.map((h) => line(h * 60, R_IN, R_OUT)),
    hours: hours.filter((h) => h % 2 === 0).map((h) => ({ ...at(h * 60, R_OUT + 26), text: String((18 + h) % 24).padStart(2, '0') })),
    grid: [1, 2, 3, 4, 5].map((i) => round(R_IN + ((R_OUT - R_IN) * i) / 6)),
    flatBy: line(FLAT_BY_MIN, R_IN, R_OUT),
    rings,
    overnight: path(overnight),
    live: path(live),
    now: xy.at(-1) ?? null,
    ticks,
    hub: R_IN * 0.9,
    pnl: todayPnl === null ? '—' : usd(todayPnl, true),
  }
}

/** The dial's `aria-label`: what it plots, in words. */
export function dialLabel(s: Session, todayPnl: number | null): string {
  const parts = [`today ${todayPnl === null ? 'unknown' : usd(todayPnl, true)}`, `floor ${usd(s.floor, true)}`]
  if (s.safety > s.floor) parts.push(`safety ${usd(s.safety, true)}`)
  parts.push(`open ${usd(s.start, true)}`)
  if (s.target !== null) parts.push(`target ${usd(s.target, true)}`)
  return `Session dial: ${parts.join(', ')}`
}
