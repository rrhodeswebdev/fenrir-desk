import { type ReactNode, useEffect, useState } from 'react'
import type { Tone } from '@/components/state-text'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { type Cell, type LiveStatus, age, alarmName, killCounts, nyTime, stripLabel, systemStrip } from './model'

/** The current time, ticking each second. */
export function useNow(): number {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  return now
}

// Steel normal, Unlit idle, Ember caution, Red alarm (DESIGN.md "Code Strip").
const cellClass: Record<Tone, string> = { normal: 'bg-ink-2', idle: 'bg-idle', caution: 'bg-ember', alarm: 'bg-red' }

/** A fixed-order row of state cells; `large` is the 40x14 size of the top strip and focus. */
export function CodeStrip({ cells, large }: { cells: Cell[]; large?: boolean }) {
  return (
    <div role="img" aria-label={stripLabel(cells)} className="flex gap-1">
      {cells.map((c) => (
        <i key={c.name} data-state={c.state} className={cn('block', large ? 'h-3.5 w-10' : 'h-2.5 w-[22px]', cellClass[c.state])} />
      ))}
    </div>
  )
}

/** Label over Value, as every fact in the top strip. */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1.5">
      <span className="label">{label}</span>
      <div className="flex items-baseline gap-2">{children}</div>
    </div>
  )
}

/** New York clock, the connections and the Feed · Broker · Drift strip with the alarming account beside it.
 * `status` is undefined while it loads or when it failed (`failed`). */
export function SystemStrip({ status, failed }: { status: LiveStatus | undefined; failed: boolean }) {
  const now = useNow()
  const bar = status?.lse.last_bar_ts ?? null
  const alarm = status && alarmName(status.accounts)
  return (
    <>
      <Fact label="New York">
        <span className="font-display text-[20px] leading-none font-medium tracking-[0.04em]">{nyTime(now)}</span>
      </Fact>
      <div role="group" aria-label="Connections" className="flex items-center gap-6 max-md:flex-wrap max-md:gap-4">
        <Fact label="LSE feed">
          <span className="value">{bar ? nyTime(Date.parse(bar)) : '—'}</span>
          {status && <span className="label">{age(bar, now) ?? 'no bar yet'}</span>}
        </Fact>
        <Fact label="NinjaTrader">
          <span className="value">{age(status?.nt.last_heartbeat ?? null, now) ?? '—'}</span>
          <span className="label">heartbeat</span>
        </Fact>
        <Fact label="Feed · Broker · Drift">
          {status ? (
            <div className="flex items-center gap-2">
              <CodeStrip cells={systemStrip(status)} large />
              {alarm && <span className="label text-red">{alarm}</span>}
            </div>
          ) : (
            <span className={cn('label', failed && 'text-red')}>{failed ? 'Status unavailable' : 'Loading…'}</span>
          )}
        </Fact>
      </div>
    </>
  )
}

/** "Kill all": flatten all, cancel all, disarm all, after one AlertDialog. Usable whatever the status read. */
export function KillSwitch({ status, pending, onKill }: { status: LiveStatus | undefined; pending: boolean; onKill: () => void }) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="destructive" className="ml-auto" disabled={pending}>
          {pending ? 'Killing…' : 'Kill all'}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Kill all accounts</AlertDialogTitle>
          <AlertDialogDescription>
            Flatten every position, cancel every working order, disarm every account. Armed strategies stop until you arm them
            again.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {status ? (
          <ul className="value grid gap-1.5">
            {killCounts(status.accounts).map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        ) : (
          <p className="text-ember">The status did not load, so the counts are unknown. The kill switch still flattens and disarms everything.</p>
        )}
        <AlertDialogFooter className="mt-2">
          <AlertDialogAction variant="destructive" onClick={onKill}>
            Flatten and disarm all
          </AlertDialogAction>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
