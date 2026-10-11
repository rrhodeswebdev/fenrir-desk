import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createRoute } from '@tanstack/react-router'
import { useEffect, useRef } from 'react'
import { apiFetch } from '@/api'
import { Page } from '@/components/page'
import { StateText, type Tone } from '@/components/state-text'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { errorText, pageError } from '@/errorText'
import { rootRoute } from './root'

// GET /api/data (docs/contracts.md "Plan 16 — data API")
type Coverage = { series: string; first_day: string | null; last_day: string | null; days: number; gaps: [string, string][] }
type Roll = { series: string; start: string; contract: string }
type PullState = 'idle' | 'running' | 'done' | 'error'
type Pull = { state: PullState; started_at: string | null; finished_at: string | null; returncode: number | null; log_tail: string[] }
export type Overview = { series: Coverage[]; rolls: Roll[]; front: Record<string, string | null>; pull: Pull }

export type RollRow = { start: string; contracts: Record<string, string>; mark: 'front' | 'next' | null }

/** One row per roll date (ES and NQ roll together), marking today's front month and the next roll. */
export function rollRows(rolls: Roll[], front: Overview['front']): RollRow[] {
  // The first roll per series is synthetic (Jan 1 of first_year), not a real roll date.
  const first = new Map<string, string>()
  for (const r of rolls) {
    const earliest = first.get(r.series)
    if (earliest === undefined || r.start < earliest) first.set(r.series, r.start)
  }
  const byStart = new Map<string, Record<string, string>>()
  for (const r of rolls)
    if (r.start !== first.get(r.series)) byStart.set(r.start, { ...byStart.get(r.start), [r.series]: r.contract })
  const rows = [...byStart].sort(([a], [b]) => a.localeCompare(b))
  const current = rows.findIndex(([, contracts]) => Object.entries(contracts).some(([s, c]) => front[s] === c))
  return rows.map(([start, contracts], i) => ({
    start,
    contracts,
    mark: current < 0 ? null : i === current ? 'front' : i === current + 1 ? 'next' : null,
  }))
}

/** Poll every 2 s only while a pull runs; stop on a failed GET (a window refocus retries). */
export const pollMs = ({ data, status }: { data?: Overview; status: string }): number | false =>
  status !== 'error' && data?.pull.state === 'running' ? 2000 : false

export const gapText = (gaps: Coverage['gaps']): string =>
  gaps.length === 0 ? 'None' : gaps.map(([a, b]) => (a === b ? a : `${a} → ${b}`)).join(', ')

// Colour only for abnormal state: a running or finished pull is normal, a failed one is alarm.
const pullTone: Record<PullState, Tone> = { idle: 'idle', running: 'normal', done: 'normal', error: 'alarm' }

export const dataRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/data',
  component: DataPage,
})

function DataPage() {
  const data = useQuery({
    queryKey: ['data'],
    queryFn: () => apiFetch<Overview>('/api/data'),
    refetchInterval: (q) => pollMs(q.state),
  })
  // A failed refetch keeps the last data on screen, so the error shows above it too.
  const message = pageError(data.error)
  const error = message && (
    <p role="alert" className="text-red">
      {message}
    </p>
  )
  if (!data.data)
    return (
      <Page title="Data">
        {error}
        {data.isPending && <p className="text-ink-3">Loading…</p>}
      </Page>
    )
  const { series, rolls, front, pull } = data.data
  return (
    <Page
      title="Data"
      strip={Object.entries(front).map(([s, c]) => (
        <div key={s} className="grid gap-1">
          <span className="label">Front {s}</span>
          <span className="value">{c ?? '—'}</span>
        </div>
      ))}
    >
      <div className="grid gap-8">
        {error}
        <section aria-labelledby="coverage" className="grid gap-3">
          <h2 id="coverage">Coverage · 1-minute bars</h2>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Series</TableHead>
                <TableHead scope="col">First day</TableHead>
                <TableHead scope="col">Last day</TableHead>
                <TableHead scope="col" className="text-right">
                  Days
                </TableHead>
                <TableHead scope="col">Gaps</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {series.map((c) => (
                <TableRow key={c.series}>
                  <TableCell className="value">{c.series}</TableCell>
                  <TableCell>{c.first_day ?? '—'}</TableCell>
                  <TableCell>{c.last_day ?? '—'}</TableCell>
                  <TableCell className="text-right">{c.days}</TableCell>
                  <TableCell className={c.gaps.length ? undefined : 'text-ink-3'}>{gapText(c.gaps)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="text-ink-3">MES and MNQ use the ES and NQ prices.</p>
        </section>

        <PullSection pull={pull} />

        <Rollover rolls={rolls} front={front} />
      </div>
    </Page>
  )
}

function PullSection({ pull }: { pull: Pull }) {
  const qc = useQueryClient()
  const start = useMutation({
    mutationFn: () => apiFetch<void>('/api/data/pull', { method: 'POST' }),
    onSettled: () => qc.invalidateQueries({ queryKey: ['data'] }),
  })
  // A 409 or other POST error is stale once the pull state moves on.
  useEffect(() => start.reset(), [pull.state])
  return <PullView pull={pull} error={start.error} pending={start.isPending} onPull={() => start.mutate()} />
}

export function PullView({ pull, error, pending, onPull }: { pull: Pull; error: unknown; pending: boolean; onPull: () => void }) {
  const running = pull.state === 'running'
  return (
    <section aria-labelledby="pull" className="grid gap-3 border-t pt-8">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <h2 id="pull">Pull · LSE</h2>
        <span role="status">
          <StateText tone={pullTone[pull.state]}>
            {pull.state}
            {pull.state === 'error' && ` · exit ${pull.returncode}`}
          </StateText>
        </span>
        {pull.started_at && (
          <span className="flex items-baseline gap-2">
            <span className="label">Started</span>
            <span className="value">{pull.started_at}</span>
          </span>
        )}
        {pull.finished_at && (
          <span className="flex items-baseline gap-2">
            <span className="label">Finished</span>
            <span className="value">{pull.finished_at}</span>
          </span>
        )}
        <Button className="ml-auto" onClick={onPull} disabled={running || pending}>
          {running ? 'Pulling' : 'Pull'}
        </Button>
      </div>
      {error != null && (
        <p role="alert" className="text-red">
          {errorText(error)}
        </p>
      )}
      {pull.log_tail.length > 0 && (
        <pre role="region" aria-label="Pull log" tabIndex={0} className="max-h-80 overflow-auto border p-4 text-[12px] text-ink-2">{pull.log_tail.join('\n')}</pre>
      )}
    </section>
  )
}

function Rollover({ rolls, front }: Pick<Overview, 'rolls' | 'front'>) {
  const rows = rollRows(rolls, front)
  const series = Object.keys(front)
  const current = rows.find((r) => r.mark === 'front')?.start
  const box = useRef<HTMLDivElement>(null)
  // The calendar spans decades: open it with today's front month near the middle, on a
  // whole row so no part row peeks out under the sticky head.
  useEffect(() => {
    const el = box.current
    const rows = [...(el?.querySelectorAll<HTMLElement>('tbody tr') ?? [])]
    const i = rows.findIndex((r) => r.hasAttribute('aria-current'))
    const row = rows[i]
    if (!el || !row) return
    const head = el.querySelector('thead')?.offsetHeight ?? 0
    const above = Math.floor((el.clientHeight - head) / 2 / row.offsetHeight)
    el.scrollTop = (rows[Math.max(0, i - above)]?.offsetTop ?? 0) - head
  }, [current])
  return (
    <section className="grid gap-3 border-t pt-8">
      <h2>Rollover calendar</h2>
      {Object.values(front).some((c) => c === null) && (
        <p className="text-ember">Today is outside the rollover calendar: raise last_year in instruments.yaml.</p>
      )}
      {/* The box scrolls, not the table's own container, so the head can stick (inset hairline: collapsed borders do not stick). */}
      <div
        ref={box}
        role="region"
        aria-label="Rollover calendar"
        tabIndex={0}
        className="max-h-96 overflow-y-auto border-b [&_thead]:sticky [&_thead]:top-0 [&_thead]:bg-ground [&_thead]:shadow-[inset_0_-1px_var(--rule)] [&>[data-slot=table-container]]:overflow-visible"
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Front from</TableHead>
              {series.map((s) => (
                <TableHead key={s} scope="col">
                  {s}
                </TableHead>
              ))}
              <TableHead scope="col">
                <span className="sr-only">Mark</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow
                key={r.start}
                aria-current={r.mark === 'front' ? 'date' : undefined}
                data-state={r.mark === 'front' ? 'selected' : undefined}
              >
                <TableCell>{r.start}</TableCell>
                {series.map((s) => (
                  <TableCell key={s}>
                    {r.contracts[s] ?? '—'}
                  </TableCell>
                ))}
                <TableCell>
                  {r.mark === 'front' && <StateText tone="caution">Front</StateText>}
                  {r.mark === 'next' && <StateText tone="idle">Next roll</StateText>}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  )
}
