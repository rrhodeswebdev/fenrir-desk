// Report figures as text (DESIGN.md: tabular, a sign instead of colour). Fixed locale so tests are stable.
// pct and money come from Compare.tsx so a figure reads the same on both pages (PRODUCT.md principle 4).
import { money as usd } from './Compare'

export { pct } from './Compare'

/** null (nothing to take a median of) shows as a dash. */
export const num = (x: number | null): string =>
  x === null ? '—' : x.toLocaleString('en-US', { maximumFractionDigits: 1 })

/** Compare's signed dollars; null (nothing to take a median of) shows as a dash. */
export const money = (x: number | null): string => (x === null ? '—' : usd(x))

/** A strategy PARAMS value: strings as written, anything else as JSON. */
export const paramText = (v: unknown): string => (typeof v === 'string' ? v : JSON.stringify(v))
