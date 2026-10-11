import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, createRoute } from '@tanstack/react-router'
import { useId, useState, type ReactNode } from 'react'
import { apiFetch } from '@/api'
import { Page } from '@/components/page'
import { StateText, type Tone } from '@/components/state-text'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { errorText, pageError } from '@/errorText'
import { errorsAt, fieldErrors, getIn, removeAt, setIn, type FieldError, type Path } from '@/rulesForm'
import { rootRoute } from './root'

/** One `GET /api/rules` row (contracts: Plan 15). */
export type RuleRow = {
  id: string
  verified_on: string | null
  age_days: number | null
  stale: boolean | null
  error: string | null
}

/** The row's status: Red for a file that does not load, Ember when never verified, future-dated or older than 30 days. */
export function freshness(row: RuleRow): { tone: Tone; text: string } {
  if (row.error) return { tone: 'alarm', text: 'Invalid file' }
  if (row.verified_on === null || row.age_days === null) return { tone: 'caution', text: 'Never verified' }
  const age = row.age_days
  if (age < 0) return { tone: 'caution', text: 'Verified in the future' }
  const text = age === 0 ? 'Verified today' : `Verified ${age} ${age === 1 ? 'day' : 'days'} ago`
  return { tone: row.stale ? 'caution' : 'normal', text }
}

/** `GET /api/rules/{id}`: the row plus the rules object, null when the file does not validate. */
type RuleDetail = RuleRow & { rules: Record<string, unknown> | null }

/** The draft being edited, the server copy it started from, and the last save's field errors. */
type Form = { rules: unknown; server: unknown; errors: FieldError[]; set: (path: Path, value: unknown) => void }

type ControlProps = { id: string; 'aria-invalid': boolean; 'aria-describedby'?: string }

/** One labelled row; a server error whose loc is `path` shows in Red under the control. */
function Field({ f, path, label, control }: { f: Form; path: Path; label: string; control: (p: ControlProps) => ReactNode }) {
  const id = useId()
  const errors = errorsAt(f.errors, path)
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_14rem] items-center gap-x-4 gap-y-1 py-1">
      <label htmlFor={id} className="label">
        {label}
      </label>
      {control({ id, 'aria-invalid': errors.length > 0, 'aria-describedby': errors.length ? `${id}-error` : undefined })}
      {errors.length > 0 && (
        <p id={`${id}-error`} className="col-start-2 text-red">
          {errors.join(' ')}
        </p>
      )}
    </div>
  )
}

// An emptied number stays '' (not null), so the server reports it at its field instead of reading "none".
const numberValue = (raw: string) => (raw === '' ? '' : Number(raw))
const shown = (v: unknown) => (v == null ? '' : String(v))

function Num({ f, path, label }: { f: Form; path: Path; label: string }) {
  return (
    <Field f={f} path={path} label={label} control={(p) => (
      <Input {...p} type="number" step="any" value={shown(getIn(f.rules, path))} onChange={(e) => f.set(path, numberValue(e.target.value))} />
    )} />
  )
}

/** A number the schema allows to be null: "None" sets null; clearing it brings back the server value, or an empty field. */
function NullableNum({ f, path, label }: { f: Form; path: Path; label: string }) {
  const value = getIn(f.rules, path)
  return (
    <Field f={f} path={path} label={label} control={(p) => (
      <div className="flex items-center gap-3">
        <Input {...p} type="number" step="any" disabled={value === null} value={shown(value)} onChange={(e) => f.set(path, numberValue(e.target.value))} />
        <label className="label flex shrink-0 cursor-pointer items-center gap-2">
          <Checkbox aria-label={`${label}: none`} checked={value === null} onCheckedChange={(c) => f.set(path, c === true ? null : (getIn(f.server, path) ?? ''))} />
          None
        </label>
      </div>
    )} />
  )
}

function Text({ f, path, label, type = 'text', empty = '' }: { f: Form; path: Path; label: string; type?: string; empty?: string | null }) {
  return (
    <Field f={f} path={path} label={label} control={(p) => (
      <Input {...p} type={type} value={shown(getIn(f.rules, path))} onChange={(e) => f.set(path, e.target.value === '' ? empty : e.target.value)} />
    )} />
  )
}

function Bool({ f, path, label }: { f: Form; path: Path; label: string }) {
  return (
    <Field f={f} path={path} label={label} control={(p) => (
      <Switch {...p} checked={getIn(f.rules, path) === true} onCheckedChange={(c) => f.set(path, c)} />
    )} />
  )
}

function Trailing({ f, path }: { f: Form; path: Path }) {
  return (
    <Field f={f} path={path} label="Drawdown trails" control={(p) => (
      <Select value={shown(getIn(f.rules, path))} onValueChange={(v) => f.set(path, v)}>
        <SelectTrigger {...p} className="w-full">
          <SelectValue placeholder="Choose" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="end_of_day">End of day</SelectItem>
          <SelectItem value="intraday">Intraday</SelectItem>
        </SelectContent>
      </Select>
    )} />
  )
}

/** Errors at an object's own loc (a model check such as locks_at below amount). */
function Errors({ f, path }: { f: Form; path: Path }) {
  const errors = errorsAt(f.errors, path)
  return errors.length > 0 && <p className="text-red">{errors.join(' ')}</p>
}

/** A block the schema allows to be null: checking `none` sets null; clearing it brings back the server block, or `empty`. */
function Optional({ f, path, none, empty, children }: { f: Form; path: Path; none: string; empty: unknown; children: ReactNode }) {
  const id = useId()
  const on = getIn(f.rules, path) != null
  return (
    <>
      <div className="flex items-center gap-2 py-2">
        <Checkbox id={id} checked={!on} onCheckedChange={(c) => f.set(path, c === true ? null : (getIn(f.server, path) ?? empty))} />
        <label htmlFor={id} className="label cursor-pointer">
          {none}
        </label>
      </div>
      {on && children}
      <Errors f={f} path={path} />
    </>
  )
}

function Section({ legend, children }: { legend: string; children: ReactNode }) {
  return (
    <fieldset className="border-t pt-4 pb-6">
      {/* floated so the legend sits inside the fieldset, below its top rule */}
      <legend className="label float-left mb-2 w-full text-ink">{legend}</legend>
      <div className="clear-both">{children}</div>
    </fieldset>
  )
}

function PhaseFields({ f, phase }: { f: Form; phase: 'evaluation' | 'funded' }) {
  const at = (...keys: string[]): Path => [phase, ...keys]
  return (
    <>
      {phase === 'evaluation' && <Num f={f} path={at('profit_target')} label="Profit target" />}
      <Num f={f} path={at('max_drawdown', 'amount')} label="Max drawdown" />
      <Trailing f={f} path={at('max_drawdown', 'trailing')} />
      <NullableNum f={f} path={at('max_drawdown', 'locks_at')} label="Drawdown locks at" />
      <Errors f={f} path={at('max_drawdown')} />
      <NullableNum f={f} path={at('daily_loss_limit')} label="Daily loss limit" />
      <Optional f={f} path={at('consistency')} none="No consistency rule" empty={{ max_day_pct: '' }}>
        <Num f={f} path={at('consistency', 'max_day_pct')} label="Max day % of profit" />
      </Optional>
      <Num f={f} path={at('min_trading_days')} label="Min trading days" />
      <Errors f={f} path={[phase]} />
    </>
  )
}

// What a new evaluation starts as: required values empty, so the server asks for each one.
const EMPTY_EVALUATION = {
  profit_target: '',
  max_drawdown: { amount: '', trailing: '', locks_at: null },
  daily_loss_limit: null,
  consistency: null,
  min_trading_days: 0,
}

type Tier = { from_profit: unknown; max_minis: unknown }
const SCALING: Path = ['funded', 'scaling']
const NEW_TIER: Tier = { from_profit: '', max_minis: '' }

/** funded.scaling, one row pair per tier; a tier-order error shows at the list's own loc (Optional's Errors). */
function ScalingTiers({ f }: { f: Form }) {
  const tiers = (getIn(f.rules, SCALING) ?? []) as Tier[]
  return (
    <>
      {tiers.map((_, i) => (
        <div key={i} className="border-b py-2">
          <Num f={f} path={[...SCALING, i, 'from_profit']} label={`Tier ${i + 1} from profit`} />
          <Num f={f} path={[...SCALING, i, 'max_minis']} label={`Tier ${i + 1} max minis`} />
          <Errors f={f} path={[...SCALING, i]} />
          {/* the last tier goes with "No scaling", so the list is never empty */}
          <Button type="button" size="sm" disabled={tiers.length === 1} onClick={() => f.set(SCALING, removeAt(tiers, i))}>
            Remove tier {i + 1}
          </Button>
        </div>
      ))}
      <Button type="button" size="sm" className="mt-2" onClick={() => f.set(SCALING, [...tiers, NEW_TIER])}>
        Add tier
      </Button>
    </>
  )
}

/** Every schema field, field by field. */
function RulesFields({ f }: { f: Form }) {
  return (
    <>
      <Section legend="Account">
        <Text f={f} path={['verified_on']} label="Verified on" type="date" empty={null} />
        <Text f={f} path={['source_url']} label="Source URL" />
        <Bool f={f} path={['automation_allowed']} label="Automation allowed" />
      </Section>
      <Section legend="Evaluation">
        <Optional f={f} path={['evaluation']} none="No evaluation" empty={EMPTY_EVALUATION}>
          <PhaseFields f={f} phase="evaluation" />
        </Optional>
      </Section>
      <Section legend="Funded">
        <PhaseFields f={f} phase="funded" />
      </Section>
      <Section legend="Payout">
        <Num f={f} path={['funded', 'payout', 'max_pct_of_profit']} label="Max % of profit" />
        <Num f={f} path={['funded', 'payout', 'split_pct']} label="Split %" />
        <Num f={f} path={['funded', 'payout', 'min_days']} label="Min days" />
        <Num f={f} path={['funded', 'payout', 'min_profit']} label="Min request" />
        <Num f={f} path={['funded', 'payout', 'min_day_profit']} label="Min day profit" />
        <NullableNum f={f} path={['funded', 'payout', 'max_amount']} label="Max per payout" />
        <NullableNum f={f} path={['funded', 'payout', 'max_payouts']} label="Max payouts" />
        <Errors f={f} path={['funded', 'payout']} />
      </Section>
      <Section legend="Scaling">
        <Optional f={f} path={SCALING} none="No scaling (max minis throughout)" empty={[{ ...NEW_TIER, from_profit: 0 }]}>
          <ScalingTiers f={f} />
        </Optional>
      </Section>
      <Section legend="Session">
        <Text f={f} path={['session', 'flat_by']} label="Flat by (HH:MM)" />
        <Text f={f} path={['session', 'tz']} label="Time zone" />
      </Section>
      <Section legend="Contracts">
        <Num f={f} path={['contracts', 'max_minis']} label="Max minis" />
        <Num f={f} path={['contracts', 'micros_per_mini']} label="Micros per mini" />
      </Section>
      <Section legend="Prohibited">
        <Bool f={f} path={['prohibited', 'hft']} label="HFT prohibited" />
        <Optional f={f} path={['prohibited', 'microscalp']} none="No microscalp rule" empty={{ max_profit_pct: '', hold_seconds: '' }}>
          <Num f={f} path={['prohibited', 'microscalp', 'max_profit_pct']} label="Microscalp max profit %" />
          <Num f={f} path={['prohibited', 'microscalp', 'hold_seconds']} label="Microscalp hold seconds" />
        </Optional>
      </Section>
      <Section legend="Costs">
        <Num f={f} path={['costs', 'activation_fee']} label="Activation fee" />
        {['ES', 'MES', 'NQ', 'MNQ'].map((s) => (
          <Num key={s} f={f} path={['costs', 'commission_per_side', s]} label={`${s} commission per side`} />
        ))}
      </Section>
    </>
  )
}

function RuleEditor({ id }: { id: string }) {
  const qc = useQueryClient()
  const url = `/api/rules/${encodeURIComponent(id)}`
  const detail = useQuery({ queryKey: ['rules', id], queryFn: () => apiFetch<RuleDetail>(url) })
  const [draft, setDraft] = useState<unknown>(null) // null = no edits yet
  const save = useMutation({
    mutationFn: (rules: unknown) => apiFetch<RuleRow>(url, { method: 'PUT', body: JSON.stringify(rules) }),
    onSuccess: async (_row, sent) => {
      await qc.invalidateQueries({ queryKey: ['rules'] }) // the list and this account
      setDraft((d: unknown) => (d === sent ? null : d)) // keep edits typed during the save
    },
  })
  const error = pageError(detail.error)
  const server = detail.data?.rules
  let body: ReactNode = detail.isPending && <p className="text-ink-3">Loading…</p>
  if (error) body = <p className="text-red">{error}</p>
  else if (server === null)
    body = (
      <>
        <p className="text-red">Fix this file by hand: it does not load, so it cannot be edited here.</p>
        <p className="max-w-[72ch] text-ink-2 wrap-anywhere">{detail.data?.error}</p>
      </>
    )
  else if (server) {
    const errors = fieldErrors(save.error)
    const f: Form = { rules: draft ?? server, server, errors, set: (path, value) => setDraft((d: unknown) => setIn(d ?? server, path, value)) }
    body = (
      <form
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate(f.rules)
        }}
      >
        <div className="mb-4 flex items-center gap-4">
          <Button type="submit" disabled={draft === null || save.isPending}>
            Save
          </Button>
          {save.isSuccess && draft === null && <p role="status" className="text-ink-2">Saved.</p>}
        </div>
        {save.error && (
          <div role="alert" className="mb-4 text-red">
            <p>{errorText(save.error)}</p>
            <ul>
              {errors.map((e) => (
                <li key={`${e.loc.join('.')}:${e.msg}`} className="wrap-anywhere">
                  {e.loc.join('.') || 'rules'}: {e.msg}
                </li>
              ))}
            </ul>
          </div>
        )}
        <RulesFields f={f} />
      </form>
    )
  }
  return (
    <section aria-label={`Edit ${id}`} className="min-w-0">
      <h2 className="mb-4 text-ink">{id}</h2>
      {body}
    </section>
  )
}

function RulesPage() {
  const { account } = rulesRoute.useSearch()
  const rows = useQuery({ queryKey: ['rules'], queryFn: () => apiFetch<RuleRow[]>('/api/rules') })
  const error = pageError(rows.error)
  return (
    <Page title="Rules" className={account ? 'grid items-start gap-8 p-8 max-md:px-4 min-[1180px]:grid-cols-[auto_minmax(0,1fr)]' : undefined}>
      <div className="min-w-0">
        {error && <p className="text-red">{error}</p>}
        {rows.data?.length === 0 && <p className="text-ink-2">No rule files.</p>}
        {!!rows.data?.length && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Account</TableHead>
                <TableHead scope="col" className="max-md:hidden">
                  Verified on
                </TableHead>
                <TableHead scope="col">Status</TableHead>
                <TableHead scope="col">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.data.map((r) => {
                const { tone, text } = freshness(r)
                return (
                  <TableRow key={r.id} data-state={r.id === account ? 'selected' : undefined}>
                    <TableCell className="value">{r.id}</TableCell>
                    <TableCell className={r.verified_on ? 'value max-md:hidden' : 'text-ink-3 max-md:hidden'}>{r.verified_on ?? '—'}</TableCell>
                    <TableCell className="whitespace-normal">
                      <StateText tone={tone}>{text}</StateText>
                      {r.error && <p className="max-w-[72ch] text-red wrap-anywhere">{r.error}</p>}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button asChild size="sm">
                        <Link to="/rules" search={{ account: r.id }} aria-label={`Edit ${r.id}`}>
                          Edit
                        </Link>
                      </Button>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </div>
      {account && <RuleEditor key={account} id={account} />}
    </Page>
  )
}

export const rulesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/rules',
  // ?account=<id> selects the file the editor opens (#53).
  validateSearch: (search: Record<string, unknown>): { account?: string } =>
    typeof search.account === 'string' ? { account: search.account } : {},
  component: RulesPage,
})
