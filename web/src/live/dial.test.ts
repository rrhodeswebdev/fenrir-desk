import { describe, expect, it } from 'vitest'
import { FLAT_BY_MIN, OPEN_MIN, R_IN, R_OUT, SIZE, dial, dialLabel, sessionMinute } from './dial'
import { equity } from './fixtures'
import type { Session } from './model'

const session = (over: Partial<Session> = {}): Session => ({
  start: 0,
  floor: -2000,
  target: 3000,
  safety: -400,
  equity: equity(0, 200),
  fills: ['2026-10-13T18:12:00+00:00'],
  ...over,
})

describe('session minute', () => {
  it('counts from 18:00 New York', () => {
    expect(sessionMinute('2026-10-12T22:00:00+00:00')).toBe(0) // 18:00 EDT
    expect(sessionMinute('2026-10-13T13:30:00+00:00')).toBe(OPEN_MIN) // 09:30
    expect(sessionMinute('2026-10-13T20:40:00Z')).toBe(FLAT_BY_MIN) // 16:40
    expect(sessionMinute('2026-12-14T23:05:00Z')).toBe(5) // 18:05 EST
    expect(sessionMinute('nonsense')).toBeNull()
  })
})

describe('dial', () => {
  it('puts the floor on the inner ring and the target on the outer one', () => {
    const d = dial(session(), 200, null)
    const r = Object.fromEntries(d.rings.map((x) => [x.name, x.r]))
    expect(r.floor).toBe(R_IN)
    expect(r.target).toBe(R_OUT)
    expect(r.open).toBeCloseTo(R_IN + (2000 / 5000) * (R_OUT - R_IN))
    expect(d.rings.map((x) => x.label)).toEqual(['Floor −$2,000', 'Safety −$400', 'Open $0', 'Target +$3,000'])
  })

  it('turns the safety ring and the floor at risk only when equity reaches them, or on their halts', () => {
    const risk = (pnl: number, kind: Parameters<typeof dial>[2] = null) =>
      Object.fromEntries(dial(session(), pnl, kind).rings.map((x) => [x.name, x.atRisk]))
    expect(risk(200)).toMatchObject({ floor: false, safety: false })
    expect(risk(-400)).toMatchObject({ floor: false, safety: true })
    expect(risk(0, 'safety')).toMatchObject({ safety: true })
    expect(risk(-2000)).toMatchObject({ floor: true })
    expect(risk(0, 'breach')).toMatchObject({ floor: true })
  })

  it('draws no safety ring when the safety level is the floor, and no target ring when funded', () => {
    const d = dial(session({ safety: -2000, target: null }), 0, null)
    expect(d.rings.map((x) => x.name)).toEqual(['floor', 'open'])
    // funded: the outer edge is as far above the start as the floor is below it
    expect(d.rings[1]?.r).toBeCloseTo((R_IN + R_OUT) / 2)
  })

  it('keeps ring labels above their rings and a line (28) apart, never inside the hub', () => {
    const d = dial(session({ safety: -1950 }), 0, null)
    const ys = [...d.rings].sort((a, b) => a.r - b.r).map((x) => x.y)
    for (let i = 1; i < ys.length; i++) expect(ys[i - 1]! - ys[i]!).toBeGreaterThanOrEqual(28)
    for (const r of d.rings) expect(r.y).toBeLessThanOrEqual(SIZE / 2 - r.r)
    expect(Math.max(...ys)).toBeLessThan(SIZE / 2 - d.hub)
  })

  it('splits the trace at the open: dimmed overnight, then the live segment ending at the now dot', () => {
    const d = dial(session(), 200, null)
    expect(d.overnight.startsWith('M')).toBe(true)
    expect(d.live.startsWith('M')).toBe(true)
    // the two segments join: the live one starts where the overnight one ends
    expect(d.live.split(' L')[0]?.slice(1)).toBe(d.overnight.split('L').at(-1)?.trim())
    const last = d.live.split('L').at(-1)?.trim().split(' ').map(Number)
    expect([d.now?.x, d.now?.y]).toEqual(last)
    expect(d.ticks).toHaveLength(1)
    expect(d.pnl).toBe('+$200')
  })

  it('has no live segment before the open and no dot without points', () => {
    const night = dial(session({ equity: equity(0, 0).slice(0, 10) }), 0, null)
    expect(night.live).toBe('')
    expect(night.overnight).not.toBe('')
    const empty = dial(session({ equity: [], fills: ['2026-10-13T18:12:00+00:00'] }), null, null)
    expect(empty.now).toBeNull()
    expect(empty.ticks).toEqual([])
    expect(empty.pnl).toBe('—')
  })

  it('ends the trace at the stricter equity (the hub figure), never above it', () => {
    const app = dial(session(), 200, null) // the app's own marks land on +200 too
    const strict = dial(session(), 150, null) // NinjaTrader's figures are worse
    expect(strict.live.split('L').length).toBe(app.live.split('L').length + 1)
    expect(strict.now!.y).toBeGreaterThan(app.now!.y) // nearer the centre: lower equity (12 o'clock side)
    expect(Math.hypot(strict.now!.x - SIZE / 2, strict.now!.y - SIZE / 2)).toBeCloseTo(R_IN + (2150 / 5000) * (R_OUT - R_IN), 0)
  })

  it('places a winter session by New York winter time (one offset per session, read from its own times)', () => {
    const at = (ts: string, open: string) => dial(session({ equity: [[open, 0], [ts, 0]], fills: [] }), 0, null).now
    // 09:30 New York: 13:30 UTC in October (EDT), 14:30 UTC in December (EST): the same place on the dial
    const summer = at('2026-10-13T13:30:00+00:00', '2026-10-12T22:00:00+00:00')
    const winter = at('2026-12-15T14:30:00+00:00', '2026-12-14T23:00:00+00:00')
    expect(winter).toEqual(summer)
    expect(winter).not.toEqual(at('2026-12-15T13:30:00+00:00', '2026-12-14T23:00:00+00:00'))
  })

  it('has 23 hour spokes, even hours labelled from 18', () => {
    const d = dial(session(), 0, null)
    expect(d.spokes).toHaveLength(23)
    expect(d.hours.map((h) => h.text)).toEqual(['18', '20', '22', '00', '02', '04', '06', '08', '10', '12', '14', '16'])
  })

  it('says what it plots', () => {
    expect(dialLabel(session(), -812)).toBe('Session dial: today −$812, floor −$2,000, safety −$400, open $0, target +$3,000')
    expect(dialLabel(session({ safety: -2000, target: null }), null)).toBe('Session dial: today unknown, floor −$2,000, open $0')
  })
})
