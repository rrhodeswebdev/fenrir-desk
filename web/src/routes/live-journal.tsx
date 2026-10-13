import { useQuery } from '@tanstack/react-query'
import { Link, createRoute } from '@tanstack/react-router'
import { apiFetch } from '@/api'
import { Page } from '@/components/page'
import { pageError } from '@/errorText'
import { tradingDay } from '@/live/journal'
import type { LiveStatus } from '@/live/model'
import { JournalRows, journalState, useJournal } from '@/live/parts'
import { rootRoute } from './root'
import { statusPollMs } from './live'

export const liveJournalRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/live/journal',
  component: LiveJournalPage,
})

/** Today's full journal (SPEC: today's journal): every row of every armed account, bars included, newest first. */
function LiveJournalPage() {
  const status = useQuery({
    queryKey: ['live', 'status'],
    queryFn: () => apiFetch<LiveStatus>('/api/live/status'),
    refetchInterval: (q) => statusPollMs(q.state),
    retry: false,
  })
  const day = tradingDay(Date.now())
  // When the last read failed the armed accounts are unknown: show no rows rather than a stale list's.
  const fresh = status.isError ? undefined : status.data
  const accounts = fresh?.accounts.map((a) => a.account) ?? []
  const j = useJournal(accounts, day)
  const statusError = pageError(status.error)
  return (
    <Page
      title="Journal"
      strip={
        <>
          <div className="grid gap-1.5">
            <span className="label">Trading day</span>
            <span className="value">{day}</span>
          </div>
          <span className="label">All armed accounts</span>
          <Link to="/live" className="label ml-auto text-ink-2 underline decoration-rule-strong underline-offset-4">
            Live
          </Link>
        </>
      }
    >
      <div className="grid max-w-5xl gap-4">
        {statusError && (
          <p role="alert" className="text-red">
            Status: {statusError}
          </p>
        )}
        {fresh?.accounts.length === 0 && <p className="text-ink-3">No account is armed.</p>}
        {accounts.length > 0 && journalState(j, j.rows.length)}
        {j.rows.length > 0 && <JournalRows rows={j.rows} />}
      </div>
    </Page>
  )
}
