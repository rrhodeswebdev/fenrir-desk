import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createRoute } from '@tanstack/react-router'
import { ApiError, apiFetch } from '@/api'
import { Page } from '@/components/page'
import { errorText, pageError } from '@/errorText'
import { type KillResult, type LiveStatus, killText } from '@/live/model'
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

function LivePage() {
  const queryClient = useQueryClient()
  const status = useQuery({
    queryKey: ['live', 'status'],
    queryFn: () => apiFetch<LiveStatus>('/api/live/status'),
    refetchInterval: (q) => statusPollMs(q.state),
    retry: false,
  })
  const kill = useMutation({
    mutationFn: () => apiFetch<KillResult>('/api/live/kill', { method: 'POST' }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['live'] }),
  })
  // A failed refetch keeps the last status on screen, so the strip goes on showing it with the error below.
  const statusError = pageError(status.error)
  return (
    <Page
      title="Live"
      strip={
        <>
          <SystemStrip status={status.data} failed={status.isError} />
          <KillSwitch status={status.data} pending={kill.isPending} onKill={() => kill.mutate()} />
        </>
      }
    >
      <div className="grid gap-4">
        {kill.isPending && <p role="status">Kill switch running: flattening, cancelling and disarming all…</p>}
        {kill.data && (
          <p role={kill.data.ok ? 'status' : 'alert'} className={kill.data.ok ? 'text-ink-2' : 'text-red'}>
            {killText(kill.data)}
          </p>
        )}
        {kill.error && (
          <p role="alert" className="text-red">
            Kill switch request failed ({errorText(kill.error)}). It may not have run: check NinjaTrader and press it again.
          </p>
        )}
        {statusError && (
          <p role="alert" className="text-red">
            Status: {statusError}
          </p>
        )}
        {status.data?.accounts.length === 0 && <p className="text-ink-3">No account is armed.</p>}
      </div>
    </Page>
  )
}
