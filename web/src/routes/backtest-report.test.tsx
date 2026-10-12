import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import type { Period, Report } from '@/backtests/Report'
import { routeTree } from '@/router'

const KEY = 'a'.repeat(64)

const period = (over: Partial<Period> = {}): Period => ({
  start: '2019-01-02',
  end: '2025-10-09',
  attempts: 1700,
  pass_rate: 0.31,
  days_to_pass: { median: 9, histogram: [[1, 4], [3, 11]] },
  failure_reasons: { flat_by: 3, max_drawdown: 900, no_result: 20 },
  payout_rate: 0.62,
  median_days_to_payout: 14,
  median_first_payout: 1250,
  p_pass_and_payout: 0.19,
  expected_gross_per_attempt: 87.5,
  expected_net_per_attempt: -42.5,
  microscalp_flags: 4,
  sample_equity: [
    { start: '2023-05-01', outcome: 'payout', points: [['2023-05-01T20:59:00Z', 0], ['2023-05-02T20:59:00Z', 1250]] },
    { start: '2023-06-01', outcome: 'breached', points: [] },
  ],
  ...over,
})

const report = (over: Partial<Report> = {}): Report => ({
  key: KEY,
  created_at: '2026-10-10T14:00:00Z',
  strategy: 'orb',
  strategy_hash: 'h',
  params: { or_minutes: 15, side: 'long' },
  account: 'lucid-flex-50k',
  rules_hash: 'r',
  contract: 'MNQ',
  data_version: 'd',
  holdout_months: 12,
  dll_on: true,
  periods: { tuning: period(), holdout: period({ start: '2025-10-10', end: '2026-10-09', expected_net_per_attempt: 10 }) },
  holdout_gap: false,
  note: 'Attempts start on every trading day and overlap, so these rates are estimates, not exact probabilities.',
  ...over,
})

async function render(seed: (client: QueryClient) => unknown): Promise<string> {
  const client = new QueryClient()
  await seed(client)
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [`/backtests/${KEY}`] }) })
  await router.load()
  return renderToString(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

const withReport = (r: Report) => render((c) => c.setQueryData(['backtests', 'results', KEY], r))
/** The text of each cell in the table row headed `label`. */
const row = (html: string, label: string) =>
  [...(html.match(new RegExp(`scope="row"[^>]*>${label.replace(/[()]/g, '\\$&')}</th>(.*?)</tr>`))?.[1] ?? '').matchAll(/<td[^>]*>([^<]*)</g)].map((m) => m[1])

describe('report page', () => {
  it('shows tuning and hold-out side by side with the overlap note', async () => {
    const html = await withReport(report())
    expect(html).toContain('Tuning · 2019-01-02 → 2025-10-09')
    expect(html).toContain('Hold-out · 2025-10-10 → 2026-10-09')
    expect(row(html, 'P(pass and payout)')).toEqual(['19.0%', '19.0%'])
    expect(html).toContain('overlap, so these rates are estimates')
    expect(html).not.toContain('likely overfit')
  })

  it('shows gross and net per attempt, the daily loss limit and the params', async () => {
    const html = await withReport(report())
    expect(row(html, 'Gross per attempt')).toEqual(['$87.50', '$87.50'])
    expect(row(html, 'Net per attempt')).toEqual(['−$42.50', '$10.00'])
    expect(html).toMatch(/<dt class="label">Daily loss limit<\/dt><dd class="value">On<\/dd>/)
    expect(html).toMatch(/or_minutes<\/dt><dd class="value">15<\/dd>/)
    expect(html).toMatch(/side<\/dt><dd class="value">long<\/dd>/)
  })

  it('draws the days-to-pass histogram with a text alternative', async () => {
    const html = await withReport(report())
    expect(html).toContain('aria-label="Attempts by days to pass: 1 day: 4, 3 days: 11"')
    // bins 1, 2 (empty) and 3, scaled to the peak
    expect([...html.matchAll(/style="height:([^"]+)"/g)].map((m) => m[1])).toEqual(
      ['36.36363636363637%', '0%', '100%', '36.36363636363637%', '0%', '100%'],
    )
  })

  it('captions each equity sample, with a chart box only when it has points', async () => {
    const html = await withReport(report())
    expect(html).toContain('2023-05-01 · payout · 2 days · end $1,250.00 · low $0.00 · high $1,250.00')
    expect(html).toContain('2023-06-01 · breached · no closed day')
    expect(html.match(/aria-hidden="true" class="h-32 border"/g)).toHaveLength(2) // one per period
  })

  it('flags a hold-out gap and handles a run without a hold-out', async () => {
    const html = await withReport(report({ holdout_gap: true, dll_on: false, params: {}, periods: { tuning: period(), holdout: null } }))
    expect(html).toContain('likely overfit')
    expect(row(html, 'Net per attempt')).toEqual(['−$42.50', '—'])
    expect(html).toContain('No hold-out in this run.')
    expect(html).toMatch(/Daily loss limit<\/dt><dd class="value">Off<\/dd>/)
  })

  it('shows a failed refetch above the last report, keeping the key in the strip', async () => {
    const html = await render((c) => {
      c.setQueryData(['backtests', 'results', KEY], report())
      // TanStack keeps the last data beside the error
      c.getQueryCache().find({ queryKey: ['backtests', 'results', KEY] })?.setState({ status: 'error', error: new ApiError(500, '{"detail":"boom"}') })
    })
    expect(html).toContain(`<span class="value min-w-0 break-all">${KEY}</span>`)
    expect(html).toContain('<p role="alert" class="text-red">500: boom</p>')
    expect(html).toContain('Net per attempt')
  })
})
