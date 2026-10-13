import { Link, createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { pageError } from '@/errorText'
import { KillLine, LiveStrip, useLiveStatus } from '@/live/actions'
import { tradingDay } from '@/live/journal'
import { JournalRows, journalState, useJournal } from '@/live/parts'
import { rootRoute } from './root'

export const liveJournalRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/live/journal',
  component: LiveJournalPage,
})

/** Today's full journal (SPEC: today's journal): every row of every account, disarmed ones and bars included, newest
 * first, under the same top strip as `/live` (the system strip and Kill all stay in view). */
function LiveJournalPage() {
  const { status, fresh } = useLiveStatus()
  const day = tradingDay(Date.now())
  const j = useJournal(day)
  const statusError = pageError(status.error)
  return (
    <Page title="Journal" strip={<LiveStrip fresh={fresh} failed={status.isError} />}>
      <div className="grid max-w-5xl gap-4">
        <KillLine />
        {statusError && (
          <p role="alert" className="text-red">
            Status: {statusError}
          </p>
        )}
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
          <span className="label">Trading day</span>
          <span className="value">{day}</span>
          <span className="label">All accounts</span>
          <Link to="/live" className="label ml-auto text-ink-2 underline decoration-rule-strong underline-offset-4">
            Back to Live
          </Link>
        </div>
        {journalState(j, j.rows.length)}
        {j.rows.length > 0 && <JournalRows rows={j.rows} />}
      </div>
    </Page>
  )
}
