import { type MutationState, useMutation, useMutationState, useQuery, useQueryClient } from '@tanstack/react-query'
import { createRoute } from '@tanstack/react-router'
import { ApiError, apiFetch } from '@/api'
import { Page } from '@/components/page'
import { pageError } from '@/errorText'
import { KILL_TIMEOUT_MS, type KillResult, type LiveStatus, killErrorText, killText, nyTime } from '@/live/model'
import { KillSwitch, SystemStrip } from '@/live/strip'
import { rootRoute } from './root'

export const liveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/live',
  component: LivePage,
})

/** Poll every 2 s, the page's only retry, and keep polling through errors; a 401 will not fix itself. */
export const statusPollMs = ({ error }: { error: unknown }): number | false =>
  error instanceof ApiError && error.status === 401 ? false : 2000

// The kill lives in the mutation cache under this key, so leaving the page and coming back mid-kill
// still shows it running (Kill all disabled) and then its answer.
const killKey = ['live', 'kill']

function LivePage() {
  const queryClient = useQueryClient()
  const status = useQuery({
    queryKey: ['live', 'status'],
    queryFn: () => apiFetch<LiveStatus>('/api/live/status'),
    refetchInterval: (q) => statusPollMs(q.state),
    retry: false,
  })
  const kill = useMutation({
    mutationKey: killKey,
    mutationFn: () => apiFetch<KillResult>('/api/live/kill', { method: 'POST', signal: AbortSignal.timeout(KILL_TIMEOUT_MS) }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['live'] }),
  })
  const last = useMutationState<MutationState<KillResult>>({ filters: { mutationKey: killKey } }).at(-1)
  // When the last read failed the hub's state is unknown: never show the previous status as current.
  const fresh = status.isError ? undefined : status.data
  const statusError = pageError(status.error)
  return (
    <Page
      title="Live"
      strip={
        <>
          <SystemStrip status={fresh} failed={status.isError} />
          <KillSwitch status={fresh} pending={last?.status === 'pending'} onKill={() => kill.mutate()} />
        </>
      }
    >
      <div className="grid gap-4">
        {last && <KillLine run={last} />}
        {statusError && (
          <p role="alert" className="text-red">
            Status: {statusError}
          </p>
        )}
        {fresh?.accounts.length === 0 && <p className="text-ink-3">No account is armed.</p>}
      </div>
    </Page>
  )
}

/** The last kill: running, its answer, or its request error, stamped with the New York time it was pressed. */
function KillLine({ run }: { run: MutationState<KillResult> }) {
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
      <span className="mr-3 font-medium tracking-value">{nyTime(run.submittedAt)}</span>
      {text}
    </p>
  )
}
