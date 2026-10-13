import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, createRoute } from '@tanstack/react-router'
import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { ApiError, apiFetch } from '@/api'
import { money, num, paramText, pct } from '@/backtests/format'
import { Page } from '@/components/page'
import { StateText } from '@/components/state-text'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { errorText, pageError } from '@/errorText'
import { rootRoute } from './root'

// docs/contracts.md "Plans 21–24 — live API and storage": GET /api/strategies, /api/arming, /api/arming/accounts,
// POST /api/arming/arm and /api/arming/disarm.

export type Mode = 'dry' | 'sim' | 'live'
export type Phase = 'evaluation' | 'funded'

/** One backtest of a strategy file's current contents (newest first in `Strategy.reports`). */
export type StrategyReport = {
  key: string
  account: string
  contract: string
  created_at: string
  periods: { tuning: { p_pass_and_payout: number } }
}

/** One `GET /api/strategies` row; a file that fails to load has `error` and no market, params or reports. */
export type Strategy = {
  name: string
  path: string
  hash: string | null
  market: string | null
  params: Record<string, unknown>
  error: string | null
  reports: StrategyReport[]
}

/** One `GET /api/arming/accounts` row (NinjaTrader's accounts). */
export type NtAccount = { id: string; name: string; is_sim: boolean; balance: number }

/** One `GET /api/arming` row. */
export type Armed = {
  account: string
  strategy_path: string
  strategy_hash: string
  rules_account: string
  contract: string
  mode: Mode
  phase: Phase
  armed_at: string
  dll_on: boolean | null
  rules_hash: string | null
  instruments_hash: string | null
  start_balance: number | null
  peak_balance: number | null
}

/** The live confirmation: the matched backtest's tuning period plus what arming pins and the liquidation level. */
export type Summary = {
  start: string
  end: string
  attempts: number
  pass_rate: number
  p_pass_and_payout: number
  expected_gross_per_attempt: number
  expected_net_per_attempt: number
  holdout_gap: boolean
  strategy_hash: string
  rules_hash: string
  dll_on: boolean
  liquidation_level: number
}

/** An emptied balance stays '' (not null), so the server reports it at its field. */
type Balance = number | ''

export type ArmBody = {
  account: string
  strategy_path: string
  rules_account: string
  contract: string
  mode: Mode
  phase: Phase
  confirm: boolean
  start_balance?: Balance
  peak_balance?: Balance
  strategy_hash?: string
  rules_hash?: string
  dll_on?: boolean
}

/** What the form holds: `target` is the `targetKey` of a backtest's account type and contract. */
export type Draft = { strategy: string; target: string; account: string; mode: Mode; phase: Phase; start: string; peak: string }

export const EMPTY_DRAFT: Draft = { strategy: '', target: '', account: '', mode: 'dry', phase: 'evaluation', start: '', peak: '' }

export const MODES: Record<Mode, string> = { dry: 'Dry run', sim: 'Sim', live: 'Live' }
const PHASES: Record<Phase, string> = { evaluation: 'Evaluation', funded: 'Funded' }

export type Target = { account: string; contract: string }
export const targetKey = (t: Target): string => JSON.stringify([t.account, t.contract])

/** Each account type × contract a strategy has a backtest for, once, newest first: what it can be armed with. */
export function targets(reports: Target[]): Target[] {
  const out = new Map<string, Target>()
  for (const r of reports) if (!out.has(targetKey(r))) out.set(targetKey(r), { account: r.account, contract: r.contract })
  return [...out.values()]
}

const balance = (raw: string): Balance => (raw === '' ? '' : Number(raw))

/** The first arm POST. A dry run sends no balances (it starts fresh). */
export function armBody(d: Draft, target: Target): ArmBody {
  const body: ArmBody = { account: d.account, strategy_path: d.strategy, rules_account: target.account, contract: target.contract, mode: d.mode, phase: d.phase, confirm: false }
  return d.mode === 'dry' ? body : { ...body, start_balance: balance(d.start), peak_balance: balance(d.peak) }
}

/** The live confirm: the same body, confirmed, echoing what the summary showed. */
export const confirmBody = (body: ArmBody, s: Summary): ArmBody => ({ ...body, confirm: true, strategy_hash: s.strategy_hash, rules_hash: s.rules_hash, dll_on: s.dll_on })

/** An arming refusal (`{error, summary}`, a 409 or 502), or null for any other error. */
export function refusal(e: unknown): { error: string; summary: Summary | null } | null {
  if (!(e instanceof ApiError)) return null
  try {
    const body: unknown = JSON.parse(e.body)
    if (typeof body === 'object' && body !== null && 'error' in body && typeof body.error === 'string')
      return { error: body.error, summary: 'summary' in body ? (body.summary as Summary | null) : null }
  } catch {
    // not JSON: not a refusal
  }
  return null
}

/** The summary to confirm when the server asks for a live confirmation, else null. */
export const needsConfirmation = (e: unknown): Summary | null => {
  const r = refusal(e)
  return r?.error === 'needs_confirmation' ? r.summary : null
}

export const REFUSALS: Record<string, string> = {
  no_backtest: 'No backtest of this strategy file for this account type and contract. Run one first.',
  hash_changed: 'The strategy file changed since its backtests. Run the backtest again.',
  rules_changed: 'The rules file changed since its backtests. Run the backtest again.',
  dll_mismatch: "No backtest with the account type's current daily loss limit setting. Run the backtest again.",
  already_armed: 'This account is already armed. Disarm it first.',
  unknown_account: 'NinjaTrader does not report this account.',
  mode_mismatch: 'Wrong mode for this account: a sim account arms as sim, any other as live.',
  no_evaluation_phase: 'This account type has no evaluation phase. Arm it as funded.',
  nt_unavailable: 'NinjaTrader is unavailable: check that it runs with the add-on, and the address and token in Settings.',
}

/** A refusal in words, else `errorText`. */
export function armErrorText(e: unknown): string {
  const r = refusal(e)
  const text = r && REFUSALS[r.error]
  return text && e instanceof ApiError ? `${e.status}: ${text}` : errorText(e)
}

const dollars = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })
const short = (hash: string) => hash.slice(0, 12)
const fileName = (path: string) => path.split('/').pop() ?? path

/** A hash's first 12 characters; the full hash opens below them (keyboard and touch, and copyable). */
function Hash({ hash }: { hash: string }) {
  return (
    <details>
      <summary className="cursor-pointer list-none">{`${short(hash)}…`}</summary>
      <span className="break-all select-all">{hash}</span>
    </details>
  )
}

const balanceText = (b: Balance | undefined) => (typeof b === 'number' ? dollars.format(b) : '—')

/** The confirmation's figures: the tuning period's summary, the balances sent with the liquidation level they give, what arming pins and the hold-out gap. */
export function SummaryFacts({ summary: s, start, peak }: { summary: Summary; start?: Balance; peak?: Balance }) {
  const facts: [string, ReactNode][] = [
    ['Tuning period', `${s.start} to ${s.end}`],
    ['Attempts', num(s.attempts)],
    ['Pass rate', pct(s.pass_rate)],
    ['P(pass and payout)', pct(s.p_pass_and_payout)],
    ['Gross per attempt', money(s.expected_gross_per_attempt)],
    ['Net per attempt', money(s.expected_net_per_attempt)],
    ['Start balance', balanceText(start)],
    ['Peak balance', balanceText(peak)],
    ['Liquidation level', dollars.format(s.liquidation_level)],
    ['Daily loss limit', s.dll_on ? 'On' : 'Off'],
    ['Rules hash', <Hash hash={s.rules_hash} />],
    ['Strategy hash', <Hash hash={s.strategy_hash} />],
  ]
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 max-md:grid-cols-1">
      {facts.map(([label, value]) => (
        <div key={label} className="grid content-start gap-1">
          <dt className="label">{label}</dt>
          <dd className="value">{value}</dd>
        </div>
      ))}
      <div className="col-span-full grid gap-1 border-t pt-3">
        <dt className="label">Hold-out gap</dt>
        {s.holdout_gap ? (
          <dd className="text-ember">Yes. Hold-out P(pass and payout) is under half of tuning's: likely overfit.</dd>
        ) : (
          <dd className="value">None</dd>
        )}
      </div>
    </dl>
  )
}

type ControlProps = { id: string; 'aria-describedby'?: string }

/** One labelled row; `help` describes the control. */
function Field({ label, help, control }: { label: string; help?: string; control: (p: ControlProps) => ReactNode }) {
  const id = useId()
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_16rem] items-center gap-x-4 gap-y-1 py-1 max-md:grid-cols-1">
      <label htmlFor={id} className="label">
        {label}
      </label>
      {control({ id, 'aria-describedby': help ? `${id}-help` : undefined })}
      {help && (
        <p id={`${id}-help`} className="col-span-full max-w-[72ch] text-ink-3">
          {help}
        </p>
      )}
    </div>
  )
}

function Choice({ p, value, onChange, options }: { p: ControlProps; value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger {...p} className="w-full">
        <SelectValue placeholder="Choose" />
      </SelectTrigger>
      <SelectContent>
        {options.map(([v, text]) => (
          <SelectItem key={v} value={v}>
            {text}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

const ntName = (a: NtAccount) => `${a.name === a.id ? a.id : `${a.name} (${a.id})`} · ${a.is_sim ? 'sim' : 'live'}`

type Pending = { body: ArmBody; summary: Summary; stale: boolean }

/** Sends a mutation unless the last one sent through it is still in flight: `disabled` alone lets a fast double
 * click through, as it only changes on the next render. */
function useSingleFlight() {
  const busy = useRef(false)
  return <T,>(m: { mutate: (v: T, o: { onSettled: () => void }) => void }, v: T) => {
    if (busy.current) return
    busy.current = true
    m.mutate(v, { onSettled: () => (busy.current = false) })
  }
}

function ArmForm({ strategies, nt }: { strategies: Strategy[]; nt: { data?: NtAccount[]; error: unknown } }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)
  const [pending, setPending] = useState<Pending | null>(null)
  const [armed, setArmed] = useState<Armed | null>(null)
  const send = useSingleFlight()
  const cancelRef = useRef<HTMLButtonElement>(null)
  const set = (edits: Partial<Draft>) => setDraft((d) => ({ ...d, ...edits }))
  const post = (body: ArmBody) => apiFetch<Armed>('/api/arming/arm', { method: 'POST', body: JSON.stringify(body) })
  const done = (a: Armed) => {
    setArmed(a)
    void qc.invalidateQueries({ queryKey: ['arming'], exact: true })
  }
  const arm = useMutation({
    mutationFn: post,
    onMutate: () => setArmed(null),
    onSuccess: done,
    onError: (e, body) => {
      const summary = needsConfirmation(e)
      if (summary) {
        confirm.reset() // a new dialog never shows the last one's error
        setPending({ body, summary, stale: false })
      }
    },
  })
  const confirm = useMutation({
    mutationFn: post,
    onSuccess: (a) => {
      setPending(null)
      arm.reset()
      done(a)
    },
    // the strategy, rules or dll_on changed since the summary: show what is true now
    onError: (e) => {
      const summary = needsConfirmation(e)
      if (summary) setPending((p) => p && { ...p, summary, stale: true })
    },
  })
  const cancel = () => {
    setPending(null)
    confirm.reset()
  }

  const loaded = strategies.filter((s) => s.error === null)
  const strategy = loaded.find((s) => s.path === draft.strategy)
  const options = strategy ? targets(strategy.reports) : []
  const target = options.find((t) => targetKey(t) === draft.target)
  const ntError = pageError(nt.error) && armErrorText(nt.error)
  const ready = strategy && target && draft.account !== '' && !arm.isPending
  const shown = (e: unknown) => (e != null && needsConfirmation(e) === null ? armErrorText(e) : null)
  const armError = shown(arm.error)
  const confirmError = shown(confirm.error)
  const ntHelp = ntError && (nt.data ? `${ntError} The list is from the last read that worked.` : `${ntError} Type the account id to arm a dry run.`)

  // After a stale summary or a refused confirm, focus Cancel (as on open) so the keyboard stays in the dialog.
  useEffect(() => {
    if (!confirm.isPending && (pending?.stale || confirmError)) cancelRef.current?.focus()
  }, [confirm.isPending, pending, confirmError])

  return (
    <>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (ready) send(arm, armBody(draft, target))
        }}
      >
        <Field
          label="Strategy file"
          control={(p) => <Choice p={p} value={draft.strategy} onChange={(v) => set({ strategy: v, target: '' })} options={loaded.map((s) => [s.path, fileName(s.path)])} />}
        />
        <Field
          label="Backtest"
          help={strategy && options.length === 0 ? 'No backtest of this file as it is now. Run one on the Backtests page first.' : 'Account type and contract of a backtest of this file as it is now.'}
          control={(p) => <Choice p={p} value={draft.target} onChange={(v) => set({ target: v })} options={options.map((t) => [targetKey(t), `${t.account} · ${t.contract}`])} />}
        />
        <Field
          label="NinjaTrader account"
          help={ntHelp || undefined}
          control={(p) =>
            nt.data ? (
              <Choice
                p={p}
                value={draft.account}
                onChange={(id) => {
                  // prefilled with NinjaTrader's balance now; the user confirms both (DEC-4a)
                  const b = nt.data?.find((a) => a.id === id)?.balance
                  const now = b === undefined ? '' : String(b)
                  set({ account: id, start: now, peak: now })
                }}
                options={nt.data.map((a) => [a.id, ntName(a)])}
              />
            ) : (
              <Input {...p} value={draft.account} spellCheck={false} onChange={(e) => set({ account: e.target.value })} />
            )
          }
        />
        <Field label="Mode" control={(p) => <Choice p={p} value={draft.mode} onChange={(v) => set({ mode: v as Mode })} options={Object.entries(MODES)} />} />
        <Field label="Phase" control={(p) => <Choice p={p} value={draft.phase} onChange={(v) => set({ phase: v as Phase })} options={Object.entries(PHASES)} />} />
        {draft.mode !== 'dry' && (
          <>
            <Field
              label="Start balance"
              help="NinjaTrader balance at the start of this phase. Prefilled with the balance now: correct it if the phase started earlier."
              control={(p) => <Input {...p} type="number" step="any" value={draft.start} onChange={(e) => set({ start: e.target.value })} />}
            />
            <Field
              label="Peak balance"
              help="Highest end-of-day balance so far (highest intraday equity for intraday trailing). Sets the liquidation level."
              control={(p) => <Input {...p} type="number" step="any" value={draft.peak} onChange={(e) => set({ peak: e.target.value })} />}
            />
          </>
        )}
        <div className="mt-4 flex flex-wrap items-center gap-4">
          <Button type="submit" variant={draft.mode === 'live' ? 'caution' : 'outline'} disabled={!ready}>
            {`Arm ${MODES[draft.mode].toLowerCase()}`}
          </Button>
          {armed && (
            <p role="status" className="text-ink-2">
              Armed {armed.account}: {MODES[armed.mode].toLowerCase()}.
            </p>
          )}
        </div>
        {armError && (
          <p role="alert" className="mt-4 max-w-[72ch] text-red">
            {armError}
          </p>
        )}
      </form>

      <AlertDialog open={pending !== null} onOpenChange={(open) => !open && !confirm.isPending && cancel()}>
        <AlertDialogContent className="max-h-[calc(100%-2rem)] overflow-y-auto border-ember">
          {pending && (
            <>
              <AlertDialogHeader>
                <AlertDialogTitle>Arm live</AlertDialogTitle>
                <AlertDialogDescription>
                  {fileName(pending.body.strategy_path)} trades real money on {pending.body.account}: {pending.body.rules_account}, {pending.body.contract}, {PHASES[pending.body.phase].toLowerCase()}. Check the liquidation level against Lucid's dashboard.
                </AlertDialogDescription>
              </AlertDialogHeader>
              {pending.stale && (
                <p role="alert" className="text-ember">
                  Changed since you looked: the strategy, the rules or the daily loss limit setting is different now. Check the figures again.
                </p>
              )}
              <SummaryFacts summary={pending.summary} start={pending.body.start_balance} peak={pending.body.peak_balance} />
              {confirmError && (
                <p role="alert" className="text-red">
                  {confirmError}
                </p>
              )}
              <AlertDialogFooter>
                <Button variant="caution" disabled={confirm.isPending} onClick={() => send(confirm, confirmBody(pending.body, pending.summary))}>
                  Arm live
                </Button>
                <AlertDialogCancel ref={cancelRef} disabled={confirm.isPending}>
                  Cancel
                </AlertDialogCancel>
              </AlertDialogFooter>
            </>
          )}
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

/** Disarm one account, after a confirmation. */
function Disarm({ account }: { account: string }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const send = useSingleFlight()
  const refresh = () => void qc.invalidateQueries({ queryKey: ['arming'], exact: true })
  const disarm = useMutation({
    mutationFn: () => apiFetch<{ ok: boolean }>('/api/arming/disarm', { method: 'POST', body: JSON.stringify({ account }) }),
    // ok: false (the kill switch or another tab disarmed it first) stays open to say so; closing refreshes
    onSuccess: (r) => {
      if (!r.ok) return
      setOpen(false)
      refresh()
    },
  })
  return (
    <AlertDialog
      open={open}
      onOpenChange={(o) => {
        if (disarm.isPending) return
        if (!o && disarm.data?.ok === false) refresh()
        setOpen(o)
        disarm.reset()
      }}
    >
      <AlertDialogTrigger asChild>
        <Button size="sm" aria-label={`Disarm ${account}`}>
          Disarm
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Disarm {account}</AlertDialogTitle>
          <AlertDialogDescription>The runner stops trading this account within 5 seconds. An open position stays open with its broker-side stop and target.</AlertDialogDescription>
        </AlertDialogHeader>
        {(disarm.error != null || disarm.data?.ok === false) && (
          <p role="alert" className="text-red">
            {disarm.error != null ? errorText(disarm.error) : `${account} was not armed: nothing was disarmed.`}
          </p>
        )}
        <AlertDialogFooter>
          <Button variant="destructive" disabled={disarm.isPending} onClick={() => send(disarm, undefined)}>
            Disarm
          </Button>
          <AlertDialogCancel disabled={disarm.isPending}>Cancel</AlertDialogCancel>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** "2026-10-13T14:05:09.123+00:00" → "2026-10-13 14:05 UTC". */
export const armedAt = (iso: string): string => `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`

export function ArmedTable({ rows }: { rows: Armed[] }) {
  if (rows.length === 0) return <p className="text-ink-3">Nothing armed.</p>
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {['Account', 'Strategy', 'Account type', 'Contract', 'Mode', 'Phase', 'Armed'].map((h) => (
            <TableHead key={h} scope="col">
              {h}
            </TableHead>
          ))}
          <TableHead scope="col">
            <span className="sr-only">Disarm</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((a) => (
          <TableRow key={a.account}>
            <TableCell className="value">{a.account}</TableCell>
            <TableCell>{fileName(a.strategy_path)}</TableCell>
            <TableCell>{a.rules_account}</TableCell>
            <TableCell>{a.contract}</TableCell>
            <TableCell>
              <StateText tone={a.mode === 'dry' ? 'idle' : 'normal'}>{MODES[a.mode]}</StateText>
            </TableCell>
            <TableCell>{PHASES[a.phase]}</TableCell>
            <TableCell>{armedAt(a.armed_at)}</TableCell>
            <TableCell className="text-right">
              <Disarm account={a.account} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

export function StrategyTable({ rows }: { rows: Strategy[] }) {
  if (rows.length === 0) return <p className="text-ink-3">No strategy files in strategies/.</p>
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {['File', 'Market', 'Params', 'Backtests'].map((h) => (
            <TableHead key={h} scope="col">
              {h}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((s) => {
          const latest = s.reports[0]
          const params = Object.entries(s.params).map(([k, v]) => `${k}=${paramText(v)}`)
          return (
            <TableRow key={s.path}>
              <TableCell className="value">{fileName(s.path)}</TableCell>
              <TableCell>{s.market ?? '—'}</TableCell>
              <TableCell className="whitespace-normal">
                {s.error ? <p className="max-w-[72ch] text-red wrap-anywhere">{s.error}</p> : <span className="wrap-anywhere">{params.join(' ') || '—'}</span>}
              </TableCell>
              <TableCell>
                {latest ? (
                  <>
                    {`${s.reports.length} · latest `}
                    <Link to="/backtests/$key" params={{ key: latest.key }}>{`${latest.account} ${latest.contract}`}</Link>
                    {` ${pct(latest.periods.tuning.p_pass_and_payout)}`}
                  </>
                ) : (
                  <span className="text-ink-3">None</span>
                )}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

function StrategiesPage() {
  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => apiFetch<Strategy[]>('/api/strategies') })
  const armed = useQuery({ queryKey: ['arming'], queryFn: () => apiFetch<Armed[]>('/api/arming') })
  // a 502 is NinjaTrader down: say so at once, no retries
  const nt = useQuery({ queryKey: ['arming', 'accounts'], queryFn: () => apiFetch<NtAccount[]>('/api/arming/accounts'), retry: false })
  const state = (q: { isPending: boolean; error: unknown }) => {
    const error = pageError(q.error)
    return error ? <p className="text-red">{error}</p> : q.isPending && <p className="text-ink-3">Loading…</p>
  }
  return (
    <Page title="Strategies">
      <div className="grid max-w-[72rem] grid-cols-[minmax(0,1fr)] gap-10">
        <section aria-labelledby="strategies-armed">
          <h2 id="strategies-armed" className="mb-4 text-ink">
            Armed accounts
          </h2>
          {armed.data ? <ArmedTable rows={armed.data} /> : state(armed)}
        </section>
        <section aria-labelledby="strategies-arm" className="max-w-[48rem]">
          <h2 id="strategies-arm" className="mb-4 text-ink">
            Arm
          </h2>
          <p className="mb-4 max-w-[72ch] text-ink-3">Needs a backtest of the file as it is now, with the account type's rules and daily loss limit setting. A live arm shows that backtest's summary to confirm first.</p>
          {strategies.data ? <ArmForm strategies={strategies.data} nt={nt} /> : state(strategies)}
        </section>
        <section aria-labelledby="strategies-files">
          <h2 id="strategies-files" className="mb-4 text-ink">
            Strategy files
          </h2>
          {strategies.data ? <StrategyTable rows={strategies.data} /> : state(strategies)}
        </section>
      </div>
    </Page>
  )
}

export const strategiesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/strategies',
  component: StrategiesPage,
})
