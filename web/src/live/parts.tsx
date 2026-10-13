import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { type ReactNode, useMemo } from 'react'
import { apiFetch } from '@/api'
import { toneClass } from '@/components/state-text'
import { Button } from '@/components/ui/button'
import { pageError } from '@/errorText'
import { cn } from '@/lib/utils'
import { SIZE, dial, dialLabel } from './dial'
import { type JournalRow, journalLine, journalPollMs, merge, tape } from './journal'
import {
  type AccountStatus,
  type Meter,
  type Segment,
  type Session,
  SEGMENTS,
  accountStrip,
  alertTone,
  bracketText,
  canResolve,
  meterLabel,
  meters,
  modeText,
  phaseText,
  positionText,
  reasons,
  segments,
  typeText,
  usd,
} from './model'
import { CodeStrip } from './strip'

/** Resolve one account; `resolving` is the account whose Resolve is in flight, if any. */
export type ResolveProps = { onResolve: (account: string) => void; resolving: string | null }

function ResolveButton({ a, onResolve, resolving, className }: ResolveProps & { a: AccountStatus; className?: string }) {
  if (!canResolve(a)) return null
  const busy = resolving === a.account
  return (
    <Button
      variant="destructive"
      className={className}
      disabled={resolving !== null}
      aria-label={`Resolve ${a.account}`}
      onClick={() => onResolve(a.account)}
    >
      {busy ? 'Resolving…' : 'Resolve'}
    </Button>
  )
}

const C = SIZE / 2
// Rings are Slate; the floor turns Red and the safety ring Ember only when at risk; the target is Steel.
const ringClass = {
  floor: (risk: boolean) => cn('[stroke-dasharray:2_4]', risk ? 'stroke-red' : 'stroke-ink-3'),
  safety: (risk: boolean) => cn('[stroke-dasharray:8_4]', risk ? 'stroke-ember' : 'stroke-ink-3'),
  open: () => 'stroke-ink-3',
  target: () => 'stroke-ink-2',
}

/** The session dial (DESIGN.md "Session Dial"); `mini` is the sleeve's: no labels or ticks, trace at 2.5. The full
 * dial's live trace draws itself on mount (key it by account so it draws again when focus changes). The geometry is
 * memoised: a re-render without a new status (a focus change elsewhere, the clock) does not recompute it. */
export function Dial({ session, todayPnl, haltKind, mini }: { session: Session; todayPnl: number | null; haltKind: AccountStatus['halt_kind']; mini?: boolean }) {
  const d = useMemo(() => dial(session, todayPnl, haltKind), [session, todayPnl, haltKind])
  // a Night halo (paint-order: stroke, not a shadow) keeps labels legible where they cross rings, spokes and the trace
  const text = 'font-mono text-[20px] font-medium stroke-ground [paint-order:stroke] [stroke-linejoin:round] [stroke-width:8px]'
  return (
    <svg
      viewBox={`0 0 ${SIZE} ${SIZE}`}
      className={cn('aspect-square w-full overflow-visible', !mini && 'max-w-[520px]')}
      {...(mini ? { 'aria-hidden': true } : { role: 'img', 'aria-label': dialLabel(session, todayPnl) })}
    >
      {d.spokes.map((l, i) => (
        <line key={i} {...l} className={mini ? 'stroke-idle' : 'stroke-rule-strong'} />
      ))}
      {d.grid.map((r) => (
        <circle key={r} cx={C} cy={C} r={r} className={cn('fill-none', mini ? 'stroke-idle' : 'stroke-rule-strong')} />
      ))}
      {!mini &&
        d.hours.map((h) => (
          <text key={h.text} x={h.x} y={h.y + 7} textAnchor="middle" className={cn(text, 'fill-ink-3 tracking-[0.1em]')}>
            {h.text}
          </text>
        ))}
      <line {...d.flatBy} className="stroke-ink-3 [stroke-dasharray:1_3]" />
      {d.rings.map((r) => (
        <circle key={r.name} data-ring={r.name} data-at-risk={r.atRisk || undefined} cx={C} cy={C} r={r.r} className={cn('fill-none', ringClass[r.name](r.atRisk))} />
      ))}
      {!mini &&
        d.rings.map((r) => (
          <text key={r.name} x={C + 30} y={r.y} className={cn(text, 'fill-ink-2 tracking-[0.12em] uppercase')}>
            {r.label}
          </text>
        ))}
      <path d={d.overnight} strokeWidth={mini ? 2.5 : 1.2} className="fill-none stroke-ink-3 [stroke-dasharray:1_3] [stroke-opacity:0.35]" />
      <path
        data-trace="live"
        d={d.live}
        pathLength={1}
        strokeWidth={mini ? 2.5 : 1.2}
        className={cn('fill-none stroke-ink [stroke-linejoin:round]', !mini && 'animate-trace [stroke-dasharray:1] motion-reduce:animate-none')}
      />
      {!mini && d.ticks.map((l, i) => <line key={i} {...l} strokeWidth={1.5} className="stroke-ink" />)}
      {d.now && <circle cx={d.now.x} cy={d.now.y} r={mini ? 8 : 4} className="fill-ink" />}
      <circle cx={C} cy={C} r={d.hub} className="fill-ground stroke-rule-strong" />
      {!mini && (
        <>
          <text x={C} y={C + 6} textAnchor="middle" className="fill-ink font-display text-[30px] font-semibold tracking-[0.02em]">
            {d.pnl}
          </text>
          <text x={C} y={C + 34} textAnchor="middle" className={cn(text, 'fill-ink-3 tracking-label')}>
            TODAY
          </text>
        </>
      )}
    </svg>
  )
}

// Unlit, Steel lit, Ember past the safety mark, Red at 100% (DESIGN.md "Segmented Meter").
const segClass: Record<Segment['state'], string> = { idle: 'bg-idle', on: 'bg-ink-2', caution: 'bg-ember', alarm: 'bg-red' }
const unlit: Segment[] = Array.from({ length: SEGMENTS }, () => ({ state: 'idle', mark: false }))

/** Label and Value on one row above 20 cells; the safety cell carries an inset Ember edge. */
export function MeterRow({ m }: { m: Meter }) {
  const none = 'none' in m
  return (
    <div className="grid gap-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <span className="label">{m.name}</span>
        {none ? (
          <span className="value text-ink-3">{m.none}</span>
        ) : (
          <span className="value whitespace-nowrap">
            {m.figure} <span className="text-ink-3">{m.of}</span>
          </span>
        )}
      </div>
      <div role="img" aria-label={meterLabel(m)} className="grid grid-cols-[repeat(20,minmax(0,1fr))] gap-[3px]">
        {(none ? unlit : segments(m)).map((s, i) => (
          <i key={i} data-state={s.state} data-mark={s.mark || undefined} className={cn('block h-2.5', segClass[s.state], s.mark && 'safety-mark')} />
        ))}
      </div>
    </div>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="label mb-1.5">{label}</dt>
      <dd className="value">{children}</dd>
    </div>
  )
}

/** The focus field (DESIGN.md "Catalog-Number Headline", "Session Dial"): identity, dial, then facts and meters.
 * `onAuto` is set while a user's pick holds the focus: it hands focus back to the most severe account. */
export function FocusField({ a, onAuto, ...resolve }: ResolveProps & { a: AccountStatus; onAuto?: () => void }) {
  return (
    <section
      aria-label="Focused account"
      className="grid grid-cols-[minmax(280px,1fr)_minmax(360px,1.25fr)_minmax(260px,0.8fr)] gap-8 border-b p-8 max-[1181px]:grid-cols-2 max-md:grid-cols-1 max-md:px-4"
    >
      <div className="flex min-w-0 flex-col gap-6">
        <h2 className="font-display text-[clamp(56px,6.4vw,96px)] leading-[0.92] font-medium tracking-[0.01em] text-ink normal-case">
          {a.account}
        </h2>
        <p className="font-mono text-[15px] leading-[1.3] font-medium tracking-[0.3em] text-ink uppercase">{typeText(a)}</p>
        <div className="flex items-center gap-3">
          <CodeStrip cells={accountStrip(a)} large />
          <span className="label">
            {modeText[a.mode]} · {phaseText[a.phase]}
          </span>
        </div>
        {reasons(a).map((r) => (
          <p key={r.text} className={cn('max-w-[44ch]', toneClass[r.tone])}>
            {r.text}
          </p>
        ))}
        <div className="flex flex-wrap gap-3">
          <ResolveButton a={a} {...resolve} />
          {onAuto && (
            <Button onClick={onAuto} title="Focus follows the most severe account again">
              Follow most severe
            </Button>
          )}
        </div>
      </div>

      <div className="grid min-w-0 place-items-center max-[1181px]:col-span-full max-[1181px]:row-start-1 max-md:row-start-auto">
        {a.session ? (
          <Dial key={a.account} session={a.session} todayPnl={a.today_pnl} haltKind={a.halt_kind} />
        ) : (
          <p className="label">No session figures: the account did not start</p>
        )}
      </div>

      <div className="flex min-w-0 flex-col gap-6">
        <dl className="grid gap-4">
          <Fact label="Strategy">{a.strategy}</Fact>
          <Fact label="Contract">{a.contract}</Fact>
          <Fact label="Position">{positionText(a.position)}</Fact>
          <Fact label="Bracket">{bracketText(a.position)}</Fact>
          <Fact label="NinjaTrader balance">{a.nt_balance === null ? '—' : usd(a.nt_balance)}</Fact>
          <Fact label="Since phase start">{a.balance === null ? '—' : usd(a.balance, true)}</Fact>
        </dl>
        <div className="border-t" />
        <div className="grid gap-4">
          {meters(a).map((m) => (
            <MeterRow key={m.name} m={m} />
          ))}
        </div>
      </div>
    </section>
  )
}

/** One other armed account (DESIGN.md "Sleeve"); clicking it focuses it. */
function Sleeve({ a, onFocus, ...resolve }: ResolveProps & { a: AccountStatus; onFocus: (account: string) => void }) {
  const tone = alertTone(a)
  const dim = a.mode === 'dry' && 'opacity-55 grayscale'
  return (
    <article
      aria-label={a.account}
      data-tone={tone ?? undefined}
      onClick={(e) => {
        if (!(e.target as Element).closest('button')) onFocus(a.account)
      }}
      className={cn(
        'step flex min-w-0 cursor-pointer flex-col gap-3 border-r border-b p-6 hover:bg-ground-deep max-md:border-r-0',
        tone === 'alarm' && 'shadow-[inset_0_0_0_1px_var(--red)]',
        tone === 'caution' && 'shadow-[inset_0_0_0_1px_var(--ember)]',
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <button
          type="button"
          aria-label={`Focus ${a.account}`}
          onClick={() => onFocus(a.account)}
          className={cn('cursor-pointer text-left font-display text-[24px] leading-none font-medium tracking-[0.04em]', dim)}
        >
          {a.account}
        </button>
        <CodeStrip cells={accountStrip(a)} />
      </div>
      <span className="label">
        {typeText(a)} · {modeText[a.mode]}
      </span>
      {reasons(a).map((r) => (
        <p key={r.text} className={cn('font-mono text-[11px] leading-[1.4] font-medium tracking-[0.12em] uppercase', toneClass[r.tone])}>
          {r.text}
        </p>
      ))}
      {/* the facts drop below the mini dial when the sleeve is too narrow for both */}
      <div className="flex flex-wrap items-center gap-4">
        <div className={cn('w-[112px] shrink-0', dim)}>
          {a.session && <Dial session={a.session} todayPnl={a.today_pnl} haltKind={a.halt_kind} mini />}
        </div>
        <dl className="grid min-w-[150px] flex-1 grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          <dt className="label">Today</dt>
          <dd className="value text-right">{a.today_pnl === null ? '—' : usd(a.today_pnl, true)}</dd>
          <dt className="label">DD left</dt>
          <dd className="value text-right">{a.limits ? usd(a.limits.drawdown_remaining) : '—'}</dd>
          <dt className="label">Position</dt>
          <dd className="value text-right">{positionText(a.position).split(' @ ')[0]}</dd>
        </dl>
      </div>
      <ResolveButton a={a} {...resolve} className="self-start" />
    </article>
  )
}

/** Every armed account but the focused one. */
export function Sleeves({ accounts, focused, onFocus, ...resolve }: ResolveProps & { accounts: AccountStatus[]; focused: string; onFocus: (account: string) => void }) {
  const others = accounts.filter((a) => a.account !== focused)
  return (
    <div className="grid auto-rows-min grid-cols-[repeat(3,minmax(220px,1fr))] max-[1181px]:grid-cols-[repeat(auto-fit,minmax(240px,1fr))] max-md:grid-cols-1">
      {others.length ? (
        others.map((a) => <Sleeve key={a.account} a={a} onFocus={onFocus} {...resolve} />)
      ) : (
        <p className="p-6 text-ink-3">No other account armed.</p>
      )}
    </div>
  )
}

/** Today's journal of every account, disarmed ones included, newest first; polled every 5 s. `skip` leaves kinds
 * out on the server (the tape skips the per-minute bars). A failed read shows no rows: never stale ones. */
export function useJournal(day: string, skip: JournalRow['kind'][] = []) {
  const q = useQuery({
    queryKey: ['live', 'journal', day, skip],
    queryFn: () => apiFetch<JournalRow[]>(`/api/live/journal?${new URLSearchParams([['day', day], ...skip.map((k) => ['skip', k])])}`),
    refetchInterval: (query) => journalPollMs(query.state),
    retry: false,
  })
  return { rows: q.isError ? [] : merge([q.data ?? []]), error: pageError(q.error), loading: q.isPending }
}

/** Journal rows on the 72px / 1fr grid, the kind in tracked caps in its tone. */
export function JournalRows({ rows }: { rows: JournalRow[] }) {
  return (
    <ol className="grid">
      {rows.map(journalLine).map((l) => (
        <li key={l.key} data-kind={l.kind} className="grid grid-cols-[72px_minmax(0,1fr)] gap-3 border-t py-[7px] text-[12px] text-ink-2">
          <span>{l.time}</span>
          <span className="[overflow-wrap:anywhere]">
            <span className={cn('mr-2 tracking-[0.16em]', toneClass[l.tone])}>{l.kind}</span>
            {l.text}
          </span>
        </li>
      ))}
    </ol>
  )
}

/** The journal state as text, or null once there are rows. */
export function journalState(j: { rows: JournalRow[]; error: string | null; loading: boolean }, shown: number): ReactNode {
  if (j.error) return <p role="alert" className="text-red">Journal: {j.error}</p>
  if (j.loading && !j.rows.length) return <p className="label">Loading…</p>
  if (!shown) return <p className="text-ink-3">Nothing journalled today.</p>
  return null
}

/** The latest of today's events across every account, ending with the Full journal link. `alone` when no sleeves
 * stand beside it (nothing armed, or the status unreadable). */
export function JournalTape({ day, alone }: { day: string; alone?: boolean }) {
  const j = useJournal(day, ['bar', 'peak', 'day_start'])
  const rows = tape(j.rows)
  return (
    <section
      aria-labelledby="journal-title"
      className={cn(
        'flex min-w-0 flex-col gap-4 p-6 max-md:px-4',
        alone ? 'border-t' : 'border-l max-[1181px]:border-t max-[1181px]:border-l-0',
      )}
    >
      <div className="flex items-center justify-between gap-3">
        <h2 id="journal-title">Journal · today</h2>
        <span className="label">All accounts</span>
      </div>
      {journalState(j, rows.length)}
      {rows.length > 0 && <JournalRows rows={rows} />}
      <Link to="/live/journal" className="label self-start text-ink-2 underline decoration-rule-strong underline-offset-4">
        Full journal
      </Link>
    </section>
  )
}
