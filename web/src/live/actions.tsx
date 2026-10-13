// The Live pages' shared state: the status poll, and the kill switch and Resolve as keyed mutations. Both live in
// the query client's caches, so `/live` and `/live/journal` show the same status, the same kill and the same
// Resolve, and leaving a page mid-request and coming back still shows it running, then its answer.
import { type MutationState, useMutation, useMutationState, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiFetch } from '@/api'
import { toneClass } from '@/components/state-text'
import {
  KILL_TIMEOUT_MS,
  type KillResult,
  type LiveStatus,
  RESOLVE_TIMEOUT_MS,
  killErrorText,
  killText,
  nyTime,
  resolveText,
} from './model'
import { KillSwitch, SystemStrip } from './strip'

/** Poll every 2 s, the page's only retry, and keep polling through errors; a 401 will not fix itself. */
export const statusPollMs = ({ error }: { error: unknown }): number | false =>
  error instanceof ApiError && error.status === 401 ? false : 2000

/** `GET /api/live/status`, polled. `fresh` is undefined while the last read failed: the hub's state is then unknown,
 * so a page never shows the previous status as current. */
export function useLiveStatus() {
  const status = useQuery({
    queryKey: ['live', 'status'],
    queryFn: () => apiFetch<LiveStatus>('/api/live/status'),
    refetchInterval: (q) => statusPollMs(q.state),
    retry: false,
  })
  return { status, fresh: status.isError ? undefined : status.data }
}

/** The Resolve request: the account, and a client timeout, so a hung hub never leaves Resolve running for ever. */
export const resolveInit = (account: string): RequestInit => ({
  method: 'POST',
  body: JSON.stringify({ account }),
  signal: AbortSignal.timeout(RESOLVE_TIMEOUT_MS),
})

const killKey = ['live', 'kill']
const resolveKey = ['live', 'resolve']

/** The kill switch: one POST, then the status (and journal) re-read whatever came back. `last` is the latest press. */
export function useKill() {
  const queryClient = useQueryClient()
  const kill = useMutation({
    mutationKey: killKey,
    mutationFn: () => apiFetch<KillResult>('/api/live/kill', { method: 'POST', signal: AbortSignal.timeout(KILL_TIMEOUT_MS) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['live'] }),
  })
  const last = useMutationState<MutationState<KillResult>>({ filters: { mutationKey: killKey } }).at(-1)
  return { last, pending: last?.status === 'pending', kill: () => kill.mutate() }
}

/** Resolve one account, then re-read the status whatever came back: a 500 may already have lifted the pause.
 * `resolving` is the account whose Resolve is in flight; every Resolve is disabled meanwhile. */
export function useResolve() {
  const queryClient = useQueryClient()
  const resolve = useMutation({
    mutationKey: resolveKey,
    mutationFn: (account: string) => apiFetch<{ ok: boolean }>('/api/live/resolve', resolveInit(account)),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['live'] }),
  })
  const last = useMutationState<MutationState<{ ok: boolean }, Error, string>>({ filters: { mutationKey: resolveKey } }).at(-1)
  return {
    last,
    resolving: last?.status === 'pending' ? (last.variables ?? null) : null,
    onResolve: (account: string) => resolve.mutate(account),
  }
}

/** The top strip of every Live page: clock, connections, Feed · Broker · Drift and Kill all. */
export function LiveStrip({ fresh, failed }: { fresh: LiveStatus | undefined; failed: boolean }) {
  const { pending, kill } = useKill()
  return (
    <>
      <SystemStrip status={fresh} failed={failed} />
      <KillSwitch status={fresh} pending={pending} onKill={kill} />
    </>
  )
}

const Stamp = ({ at }: { at: number }) => <span className="mr-3 font-medium tracking-value">{nyTime(at)}</span>

/** The last kill: running, its answer, or its request error, stamped with the New York time it was pressed. */
export function KillLine() {
  const run = useKill().last
  if (!run) return null
  const alarm = run.status === 'error' || (run.status === 'success' && !run.data?.ok)
  const text =
    run.status === 'pending'
      ? 'Kill switch running: flattening, cancelling and disarming all…'
      : run.status === 'error'
        ? killErrorText(run.error)
        : run.data
          ? killText(run.data)
          : null
  if (text === null) return null
  return (
    <p role={alarm ? 'alert' : 'status'} className={alarm ? 'text-red' : 'text-ink-2'}>
      <Stamp at={run.submittedAt} />
      {text}
    </p>
  )
}

/** The last Resolve: running, or its answer, stamped with the New York time it was pressed (a pause can come back). */
export function ResolveLine() {
  const run = useResolve().last
  const account = run?.variables
  if (!run || !account) return null
  if (run.status === 'pending')
    return (
      <p role="status" className="text-ink-2">
        <Stamp at={run.submittedAt} />
        Resolve running for {account}: re-checking NinjaTrader…
      </p>
    )
  if (run.status !== 'success' && run.status !== 'error') return null
  const r = resolveText(account, run.data, run.error)
  return (
    <p role={r.tone === 'normal' ? 'status' : 'alert'} className={toneClass[r.tone]}>
      <Stamp at={run.submittedAt} />
      {r.text}
    </p>
  )
}
