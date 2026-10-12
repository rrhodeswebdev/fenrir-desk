// TradingView Lightweight Charts™ Copyright (c) 2026 TradingView, Inc. https://www.tradingview.com/ (Apache-2.0; its attribution logo stays on each chart)
import { ColorType, type DeepPartial, type LineData, type TimeChartOptions, type UTCTimestamp } from 'lightweight-charts'
import { money } from './format'

// One sampled attempt (docs/contracts.md PeriodResult.sample_equity): end-of-day balance since phase start.
export type EquitySample = { start: string; outcome: 'payout' | 'passed' | 'breached'; points: [string, number][] }
/** [[days to pass, attempts], ...], ascending by days. */
export type Histogram = [number, number][]

/** Every day from the first to the last bin, empty days as 0, so the bars sit on an even axis. */
export function bins(histogram: Histogram): Histogram {
  const counts = new Map(histogram)
  const first = histogram[0]?.[0]
  const last = histogram.at(-1)?.[0]
  if (first === undefined || last === undefined) return []
  return Array.from({ length: last - first + 1 }, (_, i) => [first + i, counts.get(first + i) ?? 0])
}

const days = (n: number) => `${n} ${n === 1 ? 'day' : 'days'}`

/** The histogram as words, for screen readers. */
export const histogramText = (histogram: Histogram): string =>
  histogram.map(([d, n]) => `${days(d)}: ${n}`).join(', ')

export const equitySeries = (points: EquitySample['points']): LineData<UTCTimestamp>[] =>
  points.map(([ts, value]) => ({ time: (Date.parse(ts) / 1000) as UTCTimestamp, value }))

// DESIGN.md --chart-*: Ice and Steel are neutral, Red is for a breach only.
export const outcomeColor: Record<EquitySample['outcome'], string> = {
  payout: '--chart-1',
  passed: '--chart-2',
  breached: '--chart-5',
}

/** What a sample's chart shows, in words: its caption and text alternative. */
export function equityCaption({ start, outcome, points }: EquitySample): string {
  if (points.length === 0) return `${start} · ${outcome} · no closed day`
  const values = points.map(([, v]) => v)
  return `${start} · ${outcome} · ${days(points.length)} · end ${money(values.at(-1)!)} · low ${money(Math.min(...values))} · high ${money(Math.max(...values))}`
}

/** Chart options from DESIGN.md tokens; `css` reads a custom property (the chart draws on a canvas, not with classes). */
export const chartOptions = (css: (name: string) => string, fontFamily: string): DeepPartial<TimeChartOptions> => ({
  autoSize: true,
  layout: { background: { type: ColorType.Solid, color: css('--ground') }, textColor: css('--chart-3'), fontFamily, fontSize: 11 },
  grid: { vertLines: { visible: false }, horzLines: { color: css('--rule') } },
  rightPriceScale: { borderColor: css('--rule') },
  timeScale: { borderColor: css('--rule') },
  crosshair: {
    vertLine: { color: css('--rule-strong'), labelBackgroundColor: css('--idle') },
    horzLine: { color: css('--rule-strong'), labelBackgroundColor: css('--idle') },
  },
  // a fixed sample, not a chart to explore: wheel and drag scroll the page
  handleScroll: false,
  handleScale: false,
})
