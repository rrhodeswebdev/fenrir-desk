import { LineSeries, createChart } from 'lightweight-charts'
import { useEffect, useRef } from 'react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { type EquitySample, type Histogram, bins, chartOptions, equityCaption, equitySeries, histogramText, outcomeColor } from './charts'
import { money, num, paramText, pct } from './format'

// GET /api/backtests/results/{key} (docs/contracts.md "Plans 09/10": Report, PeriodResult; net added by the API).
export type Period = {
  start: string
  end: string
  attempts: number
  pass_rate: number
  days_to_pass: { median: number | null; histogram: Histogram }
  failure_reasons: Record<string, number>
  payout_rate: number
  median_days_to_payout: number | null
  median_first_payout: number | null
  p_pass_and_payout: number
  expected_gross_per_attempt: number
  expected_net_per_attempt: number
  microscalp_flags: number
  sample_equity: EquitySample[]
}
export type Report = {
  key: string
  created_at: string
  strategy: string
  strategy_hash: string
  params: Record<string, unknown>
  account: string
  rules_hash: string
  contract: string
  data_version: string
  holdout_months: number
  dll_on: boolean
  periods: { tuning: Period; holdout: Period | null }
  holdout_gap: boolean
  note: string
}

const ROWS: [string, (p: Period) => string][] = [
  ['Attempts', (p) => num(p.attempts)],
  ['Pass rate', (p) => pct(p.pass_rate)],
  ['Median days to pass', (p) => num(p.days_to_pass.median)],
  ['Payout rate of passed', (p) => pct(p.payout_rate)],
  ['Median days to payout', (p) => num(p.median_days_to_payout)],
  ['Median first payout', (p) => money(p.median_first_payout)],
  ['P(pass and payout)', (p) => pct(p.p_pass_and_payout)],
  ['Gross per attempt', (p) => money(p.expected_gross_per_attempt)],
  ['Net per attempt', (p) => money(p.expected_net_per_attempt)],
  ['Failed: max drawdown', (p) => num(p.failure_reasons.max_drawdown ?? 0)],
  ['Failed: flat by', (p) => num(p.failure_reasons.flat_by ?? 0)],
  ['No result', (p) => num(p.failure_reasons.no_result ?? 0)],
  ['Microscalp flags', (p) => num(p.microscalp_flags)],
]

export function ReportView({ report }: { report: Report }) {
  const { tuning, holdout } = report.periods
  const params = Object.entries(report.params)
  const facts: [string, string][] = [
    ['Strategy', report.strategy],
    ['Account', report.account],
    ['Contract', report.contract],
    ['Daily loss limit', report.dll_on ? 'On' : 'Off'],
    ['Hold-out', `${report.holdout_months} months`],
    ['Created', report.created_at],
  ]
  return (
    <div className="grid gap-8">
      <dl className="flex flex-wrap gap-x-8 gap-y-4">
        {facts.map(([label, value]) => (
          <div key={label} className="grid gap-1">
            <dt className="label">{label}</dt>
            <dd className="value">{value}</dd>
          </div>
        ))}
      </dl>

      <section aria-labelledby="results" className="grid gap-3">
        <h2 id="results">Results</h2>
        {report.holdout_gap && <p className="text-ember">Hold-out P(pass and payout) is under half of tuning's: likely overfit.</p>}
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Metric</TableHead>
              <TableHead scope="col" className="text-right">
                {`Tuning · ${tuning.start} → ${tuning.end}`}
              </TableHead>
              <TableHead scope="col" className="text-right">
                {holdout ? `Hold-out · ${holdout.start} → ${holdout.end}` : 'Hold-out'}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ROWS.map(([label, cell]) => (
              <TableRow key={label}>
                <TableHead scope="row">{label}</TableHead>
                <TableCell className="text-right">{cell(tuning)}</TableCell>
                <TableCell className="text-right">{holdout ? cell(holdout) : '—'}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <p className="max-w-[60ch] text-ink-3">{report.note}</p>
      </section>

      <section aria-labelledby="params" className="grid gap-3 border-t pt-8">
        <h2 id="params">Params</h2>
        {params.length === 0 ? (
          <p className="text-ink-3">None</p>
        ) : (
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1">
            {params.map(([name, v]) => (
              <div key={name} className="contents">
                <dt className="label self-baseline">{name}</dt>
                <dd className="value">{paramText(v)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <div className="grid grid-cols-2 gap-8 border-t pt-8 max-lg:grid-cols-1">
        <PeriodDetail name="Tuning" period={tuning} />
        {holdout ? <PeriodDetail name="Hold-out" period={holdout} /> : <p className="text-ink-3">No hold-out in this run.</p>}
      </div>
    </div>
  )
}

function PeriodDetail({ name, period }: { name: string; period: Period }) {
  return (
    <section className="grid content-start gap-6">
      <h2>{name}</h2>
      <div className="grid gap-3">
        <h3>Days to pass</h3>
        <DaysToPass histogram={period.days_to_pass.histogram} median={period.days_to_pass.median} />
      </div>
      <div className="grid gap-3">
        <h3>Equity samples · end-of-day P&amp;L</h3>
        {period.sample_equity.length === 0 ? (
          <p className="text-ink-3">No samples.</p>
        ) : (
          <div className="grid grid-cols-2 gap-6 max-md:grid-cols-1">
            {period.sample_equity.map((s) => (
              <EquityChart key={`${s.start}-${s.outcome}`} sample={s} />
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function DaysToPass({ histogram, median }: { histogram: Histogram; median: number | null }) {
  const data = bins(histogram)
  const first = data[0]
  const last = data.at(-1)
  if (!first || !last) return <p className="text-ink-3">No attempt passed.</p>
  const max = Math.max(...data.map(([, n]) => n))
  return (
    <figure className="grid gap-2">
      <figcaption className="text-ink-2">
        Median {num(median)} days · {first[0]} to {last[0]} days · peak {max} attempts
      </figcaption>
      <div role="img" aria-label={`Attempts by days to pass: ${histogramText(histogram)}`} className="flex h-24 items-end gap-px border-b">
        {data.map(([d, n]) => (
          // height is data, so it stays an inline style
          <div key={d} title={`${d}d: ${n}`} className="flex-1 bg-chart-2" style={{ height: `${(n / max) * 100}%` }} />
        ))}
      </div>
      <div className="label flex justify-between">
        <span>{first[0]}d</span>
        <span>{last[0]}d</span>
      </div>
    </figure>
  )
}

function EquityChart({ sample }: { sample: EquitySample }) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = box.current
    if (!el) return
    // dark only (DESIGN.md), so the tokens are read once per chart
    const style = getComputedStyle(el)
    const css = (name: string) => style.getPropertyValue(name).trim()
    const chart = createChart(el, chartOptions(css, style.fontFamily))
    chart
      .addSeries(LineSeries, {
        color: css(outcomeColor[sample.outcome]),
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: { type: 'price', precision: 0, minMove: 1 },
      })
      .setData(equitySeries(sample.points))
    chart.timeScale().fitContent()
    return () => chart.remove()
  }, [sample])
  return (
    <figure className="grid gap-2">
      <figcaption className="text-ink-2">{equityCaption(sample)}</figcaption>
      {/* the canvas has no text: the caption above is its text alternative */}
      {sample.points.length > 0 && <div ref={box} aria-hidden="true" className="h-32 border" />}
    </figure>
  )
}
