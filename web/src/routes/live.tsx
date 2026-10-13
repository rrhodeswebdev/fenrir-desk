import { createRoute } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { Page } from '@/components/page'
import { pageError } from '@/errorText'
import { KillLine, LiveStrip, ResolveLine, useLiveStatus, useResolve } from '@/live/actions'
import { tradingDay } from '@/live/journal'
import { type Pick, focusOf, pickOf } from '@/live/model'
import { FocusField, JournalTape, Sleeves } from '@/live/parts'
import { rootRoute } from './root'

export { statusPollMs } from '@/live/actions'

export const liveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/live',
  component: LivePage,
})

function LivePage() {
  const { status, fresh } = useLiveStatus()
  const { resolving, onResolve } = useResolve()
  const resolveProps = { resolving, onResolve }
  // Only a fresh status: while the read fails, the focus field and sleeves are not shown at all.
  const accounts = fresh?.accounts ?? []
  const [pick, setPick] = useState<Pick | null>(null)
  const focus = focusOf(accounts, pick)
  // a new alarm (or the picked account gone) ends the pick: focus follows the most severe account again
  const overridden = pick !== null && fresh !== undefined && !focus?.picked
  useEffect(() => {
    if (overridden) setPick(null)
  }, [overridden])
  const statusError = pageError(status.error)
  return (
    <Page title="Live" strip={<LiveStrip fresh={fresh} failed={status.isError} />} className="">
      <div className="grid gap-4 px-8 py-6 empty:hidden max-md:px-4">
        <KillLine />
        {statusError && (
          <p role="alert" className="text-red">
            Status: {statusError}
          </p>
        )}
        <ResolveLine />
        {status.isError && <p className="text-ink-3">Accounts unavailable until the status reads again.</p>}
        {fresh?.accounts.length === 0 && <p className="text-ink-3">No account is armed.</p>}
      </div>
      {focus && <FocusField a={focus.account} onAuto={focus.picked ? () => setPick(null) : undefined} {...resolveProps} />}
      {/* the tape reads the journal on its own, so it stays after a kill or a disarm and while the status fails */}
      <section
        aria-label="Armed accounts and journal"
        className={focus ? 'grid grid-cols-[minmax(0,3fr)_minmax(320px,1.3fr)] max-[1181px]:grid-cols-1' : undefined}
      >
        {focus && (
          <Sleeves accounts={accounts} focused={focus.account.account} onFocus={(id) => setPick(pickOf(id, accounts))} {...resolveProps} />
        )}
        <JournalTape day={tradingDay(Date.now())} alone={!focus} />
      </section>
    </Page>
  )
}
