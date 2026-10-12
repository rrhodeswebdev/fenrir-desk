import { useQuery } from '@tanstack/react-query'
import { Fragment } from 'react'
import { apiFetch } from '@/api'
import { StateText } from '@/components/state-text'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { pageError } from '@/errorText'

// GET /api/backtests/compare?view=… (docs/contracts.md "Plan 14"): only the fields shown here.
export type View = 'account' | 'strategy'
export type Period = {
  p_pass_and_payout: number
  pass_rate: number
  expected_net_per_attempt: number
  microscalp_flags: number
}
export type CompareRow = {
  key: string
  strategy: string
  account: string
  contract: string
  periods: { tuning: Period; holdout: Period | null }
  holdout_gap: boolean
}
export type Group = { group: string; rows: CompareRow[] }

const FIGURES = ['P(pass+payout)', 'Pass', 'Net / attempt', 'Microscalp'] as const

/** The identity columns a row needs: the group already names the viewed one. */
export const identity = (view: View) => (['strategy', 'account', 'contract'] as const).filter((k) => k !== view)

export const pct = (x: number): string => `${(x * 100).toFixed(1)}%`

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', signDisplay: 'exceptZero' })
export const money = (x: number): string => usd.format(x)

/** A period's figures in FIGURES order; a tuning-only report has no hold-out, so dashes. */
export const figures = (p: Period | null): string[] =>
  p ? [pct(p.p_pass_and_payout), pct(p.pass_rate), money(p.expected_net_per_attempt), String(p.microscalp_flags)] : FIGURES.map(() => '—')

export const compareKey = (view: View) => ['backtests', 'compare', view]

export function Compare() {
  return (
    <section aria-labelledby="compare" className="grid gap-3 border-t pt-8">
      <h2 id="compare">Compare</h2>
      <Tabs defaultValue="account">
        <TabsList aria-label="Group by">
          <TabsTrigger value="account">By account</TabsTrigger>
          <TabsTrigger value="strategy">By strategy</TabsTrigger>
        </TabsList>
        {/* Radix mounts only the active tab, so only its view is fetched. */}
        <TabsContent value="account">
          <CompareQuery view="account" />
        </TabsContent>
        <TabsContent value="strategy">
          <CompareQuery view="strategy" />
        </TabsContent>
      </Tabs>
    </section>
  )
}

function CompareQuery({ view }: { view: View }) {
  // useJobsSocket invalidates ['backtests', 'compare'] when an item finishes, so this refetches.
  const q = useQuery({ queryKey: compareKey(view), queryFn: () => apiFetch<Group[]>(`/api/backtests/compare?view=${view}`) })
  return <CompareView view={view} groups={q.data} pending={q.isPending} error={pageError(q.error)} />
}

/** Groups and rows in server order: ranked by tuning only, never re-sorted here. */
export function CompareView({ view, groups, pending, error }: { view: View; groups?: Group[]; pending: boolean; error: string | null }) {
  const ids = identity(view)
  const width = ids.length + 2 * FIGURES.length + 1
  return (
    <div className="grid gap-3">
      {error && (
        <p role="alert" className="text-red">
          {error}
        </p>
      )}
      {pending && <p className="text-ink-3">Loading…</p>}
      {groups && groups.length === 0 && <p className="text-ink-3">No results yet.</p>}
      {groups && groups.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              {ids.map((k) => (
                <TableHead key={k} scope="col" rowSpan={2}>
                  {k}
                </TableHead>
              ))}
              <TableHead scope="colgroup" colSpan={FIGURES.length}>
                Tuning
              </TableHead>
              <TableHead scope="colgroup" colSpan={FIGURES.length}>
                Hold-out
              </TableHead>
              <TableHead scope="col" rowSpan={2}>
                Hold-out gap
              </TableHead>
            </TableRow>
            <TableRow>
              {[...FIGURES, ...FIGURES].map((f, i) => (
                <TableHead key={i} scope="col" className="text-right">
                  {f}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          {groups.map((g) => (
            <TableBody key={g.group}>
              <TableRow>
                <TableHead scope="rowgroup" colSpan={width} className="pt-6 text-ink">
                  {g.group}
                </TableHead>
              </TableRow>
              {g.rows.map((r) => (
                <TableRow key={r.key}>
                  {ids.map((k) => (
                    <TableCell key={k}>{r[k]}</TableCell>
                  ))}
                  <PeriodCells period={r.periods.tuning} />
                  <PeriodCells period={r.periods.holdout} muted />
                  <TableCell>{r.holdout_gap && <StateText tone="caution">Gap</StateText>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          ))}
        </Table>
      )}
    </div>
  )
}

// Hold-out is shown muted, never ranked on; any microscalp flag is a caution (Ember) in either period.
function PeriodCells({ period, muted = false }: { period: Period | null; muted?: boolean }) {
  return (
    <Fragment>
      {figures(period).map((text, i) => {
        const flagged = i === FIGURES.length - 1 && (period?.microscalp_flags ?? 0) > 0
        return (
          <TableCell key={i} className={`value text-right ${flagged ? 'text-ember' : muted ? 'text-ink-3' : ''}`}>
            {text}
          </TableCell>
        )
      })}
    </Fragment>
  )
}
