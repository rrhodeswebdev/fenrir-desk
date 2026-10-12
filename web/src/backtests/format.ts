// Report figures as text (DESIGN.md: tabular, a sign instead of colour). Fixed locale so tests are stable.

export { pct } from './Compare'

/** null (nothing to take a median of) shows as a dash. */
export const num = (x: number | null): string =>
  x === null ? '—' : x.toLocaleString('en-US', { maximumFractionDigits: 1 })

/** Dollars with a leading minus sign when negative; null shows as a dash. */
export const money = (x: number | null): string =>
  x === null
    ? '—'
    : `${x < 0 ? '−' : ''}$${Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

/** A strategy PARAMS value: strings as written, anything else as JSON. */
export const paramText = (v: unknown): string => (typeof v === 'string' ? v : JSON.stringify(v))
