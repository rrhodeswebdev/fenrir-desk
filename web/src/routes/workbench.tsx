import { useQuery } from '@tanstack/react-query'
import { Link, createRoute } from '@tanstack/react-router'
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { useEffect, useRef } from 'react'
import { ApiError, apiFetch, wsUrl } from '@/api'
import { type CompareRow, FIGURES, figures } from '@/backtests/Compare'
import { Page } from '@/components/page'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { pageError } from '@/errorText'
import { rootRoute } from './root'

// WS /ws/terminal (docs/contracts.md): text frames in, binary PTY output back.
export const inputFrame = (data: string): string => JSON.stringify({ type: 'input', data })
export const resizeFrame = (cols: number, rows: number): string => JSON.stringify({ type: 'resize', cols, rows })

/** The line written when the socket closes. 1000 is the shell exiting; 1006 (no close frame) is a refused handshake or a lost server; the server names other causes in the reason. */
export const closeLine = (code: number, reason: string): string => {
  if (code === 1000) return 'Disconnected: the shell exited.'
  if (code === 1006)
    return 'Disconnected: the connection closed or was refused (session expired or server stopped). Reload to start a new shell.'
  return `Disconnected: ${reason || `code ${code}`}.`
}

const token = (name: string): string => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

function TerminalPane() {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current!
    const term = new Terminal({
      fontFamily: getComputedStyle(el).fontFamily,
      fontSize: 13,
      theme: {
        background: token('--ground'),
        foreground: token('--ink-2'),
        cursor: token('--ink'),
        cursorAccent: token('--ground'),
        selectionBackground: token('--ember'),
        selectionForeground: token('--ground'),
        scrollbarSliderBackground: token('--rule-strong'),
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(el)
    fit.fit()
    const ws = new WebSocket(wsUrl('/ws/terminal'))
    ws.binaryType = 'arraybuffer'
    // keys typed while connecting wait here; the PTY holds them until the shell reads
    const queued: string[] = []
    const send = (frame: string) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(frame)
      else if (ws.readyState === WebSocket.CONNECTING) queued.push(frame)
    }
    ws.onopen = () => {
      ws.send(resizeFrame(term.cols, term.rows)) // the PTY starts at 80x24
      queued.splice(0).forEach((frame) => ws.send(frame))
    }
    ws.onmessage = (e: MessageEvent<ArrayBuffer>) => term.write(new Uint8Array(e.data))
    // shortcut: no reconnect; a reload starts a new shell.
    ws.onclose = (e) => term.write(`\r\n${closeLine(e.code, e.reason)}\r\n`)
    term.onData((data) => send(inputFrame(data)))
    term.onResize(({ cols, rows }) => send(resizeFrame(cols, rows)))
    const resize = new ResizeObserver(() => fit.fit())
    resize.observe(el)
    term.focus()
    return () => {
      resize.disconnect()
      ws.onclose = null // StrictMode: never write to the disposed terminal
      ws.close()
      term.dispose()
    }
  }, [])
  // no padding here: the fit add-on sizes the terminal to this box
  return <div ref={ref} role="group" aria-label="Terminal" data-testid="terminal" className="h-full" />
}

// GET /api/backtests/results: summaries newest first; only the fields shown here.
export type Latest = CompareRow & { created_at: string }

/** /ws/jobs only sees UI jobs, so poll to catch a report the CLI writes in the terminal; a 401 will not fix itself. */
export const latestPollMs = ({ error }: { error: unknown }): number | false =>
  error instanceof ApiError && error.status === 401 ? false : 3000

// as on Compare: hold-out muted, any microscalp flag Ember
const flagged = (p: Latest['periods']['holdout'], i: number): boolean => i === FIGURES.length - 1 && (p?.microscalp_flags ?? 0) > 0

function LatestReport() {
  // shortcut: each poll reads every cached report on the server; add a newest-only endpoint if results/ grows large.
  const q = useQuery({
    queryKey: ['backtests', 'results'],
    queryFn: () => apiFetch<Latest[]>('/api/backtests/results'),
    refetchInterval: (query) => latestPollMs(query.state),
  })
  const latest = q.data?.[0]
  const error = pageError(q.error)
  return (
    <div className="mt-4 grid gap-6">
      {error && (
        <p role="alert" className="text-red">
          {error}
        </p>
      )}
      {q.isPending && <p className="text-ink-3">Loading…</p>}
      {q.data?.length === 0 && <p className="max-w-[44ch] text-ink-2">No backtest yet. Run one from the terminal or the Backtests page.</p>}
      {latest && (
        <>
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1">
            {(
              [
                ['Strategy', latest.strategy],
                ['Account', latest.account],
                ['Contract', latest.contract],
                ['Created', latest.created_at],
              ] as const
            ).map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="label self-baseline">{label}</dt>
                <dd className="value wrap-anywhere">{value}</dd>
              </div>
            ))}
          </dl>
          {latest.holdout_gap && <p className="text-ember">Hold-out P(pass and payout) is under half of tuning's: likely overfit.</p>}
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Metric</TableHead>
                <TableHead scope="col" className="text-right">
                  Tuning
                </TableHead>
                <TableHead scope="col" className="text-right">
                  Hold-out
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {FIGURES.map((label, i) => (
                <TableRow key={label}>
                  <TableHead scope="row">{label}</TableHead>
                  <TableCell className={`value text-right ${flagged(latest.periods.tuning, i) ? 'text-ember' : ''}`}>
                    {figures(latest.periods.tuning)[i]}
                  </TableCell>
                  <TableCell className={`value text-right ${flagged(latest.periods.holdout, i) ? 'text-ember' : 'text-ink-3'}`}>
                    {figures(latest.periods.holdout)[i]}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Link to="/backtests/$key" params={{ key: latest.key }}>
            Full report
          </Link>
        </>
      )}
    </div>
  )
}

export const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: () => (
    <Page
      title="Workbench"
      className="grid h-[calc(100vh-72px)] grid-cols-[minmax(0,1fr)_360px] max-md:h-auto max-md:grid-cols-1"
    >
      <div className="min-h-0 p-4 max-md:h-[60vh]">
        <TerminalPane />
      </div>
      <section aria-labelledby="latest-backtest" className="border-l p-8 max-md:border-t max-md:border-l-0 max-md:px-4">
        <h2 id="latest-backtest">Latest backtest</h2>
        <LatestReport />
      </section>
    </Page>
  ),
})
