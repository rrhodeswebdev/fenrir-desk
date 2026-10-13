import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { createRoute } from '@tanstack/react-router'
import { useId, useState, type ReactNode } from 'react'
import { apiFetch } from '@/api'
import { Page } from '@/components/page'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { errorText, pageError } from '@/errorText'
import { errorsAt, fieldErrors, getIn, setIn, type FieldError } from '@/rulesForm'
import { rootRoute } from './root'

/** `GET /api/settings` (contracts: Plan 24): secrets arrive masked (`••••1234`). */
export type Settings = {
  lse_api_key: string
  nt_url: string
  nt_token: string
  holdout_months: number
  safety_margin_pct: number
  stale_seconds: number
  /** In Docker: `.git` entries the terminal planted below the repo root (#184). */
  nested_git: string[]
}
type Key = Exclude<keyof Settings, 'nested_git'>
/** `GET /api/settings/accounts`: account id → the user's own choices. */
export type Choices = Record<string, { eval_price: number; dll_on: boolean }>

// True today and under DEC-4d (#91: an armed account whose dll_on changes halts until re-armed); revisit when #91 lands.
export const DLL_HELP =
  'Daily loss limit: when on, backtests simulate it and hitting it halts the day; the attempt continues. Arming needs a backtest run with the current setting, so after changing it re-run the backtest and re-arm the account.'

const SECRET_NAMES = { nt_token: 'NinjaTrader token', lse_api_key: 'LSE API key' } as const
type SecretKey = keyof typeof SECRET_NAMES

type ControlProps = { id: string; 'aria-invalid': boolean; 'aria-describedby'?: string }

/** One labelled row; `unit` is part of the label, `help` (or an Ember `caution` in its place) describes the control, a server error shows in Red under it. */
function Field({ label, unit, help, caution, errors, control }: { label: string; unit?: string; help?: string; caution?: string; errors: string[]; control: (p: ControlProps) => ReactNode }) {
  const id = useId()
  const note = caution ?? help
  const describedBy = [errors.length > 0 && `${id}-error`, note && `${id}-help`].filter(Boolean).join(' ')
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_14rem] items-center gap-x-4 gap-y-1 py-1">
      <label htmlFor={id} className="grid gap-1">
        <span className="label">{label}</span>
        {unit && <span className="text-ink-3">{unit}</span>}
      </label>
      {control({ id, 'aria-invalid': errors.length > 0, 'aria-describedby': describedBy || undefined })}
      {note && (
        <p id={`${id}-help`} className={caution ? 'col-start-1 text-ember' : 'col-start-1 text-ink-3'}>
          {note}
        </p>
      )}
      {errors.length > 0 && (
        <p id={`${id}-error`} className="col-start-2 text-red">
          {errors.join(' ')}
        </p>
      )}
    </div>
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

/** The save row: Save, "Saved.", and on failure the error with each field error by loc. */
function SaveRow({ dirty, save, errors, note }: { dirty: boolean; save: { isPending: boolean; isSuccess: boolean; error: unknown }; errors: FieldError[]; note: string }) {
  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-4">
        <Button type="submit" disabled={!dirty || save.isPending}>
          Save
        </Button>
        {save.isSuccess && !dirty ? (
          <p role="status" className="text-ink-2">
            Saved.
          </p>
        ) : (
          <p className="text-ink-3">{note}</p>
        )}
      </div>
      {save.error != null && (
        <div role="alert" className="mb-4 text-red">
          <p>{errorText(save.error)}</p>
          <ul>
            {errors.map((e) => (
              <li key={`${e.loc.join('.')}:${e.msg}`} className="wrap-anywhere">
                {e.loc.join('.') || 'settings'}: {e.msg}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

// An emptied number stays '' (not null), so the server reports it at its field.
const numberValue = (raw: string) => (raw === '' ? '' : Number(raw))

function SettingsForm({ server }: { server: Settings }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Partial<Record<Key, unknown>> | null>(null) // only edited keys: PUT is partial
  const save = useMutation({
    mutationFn: (edits: Partial<Record<Key, unknown>>) => apiFetch<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(edits) }),
    onSuccess: (data, sent) => {
      qc.setQueryData(['settings'], data) // PUT responds like GET
      setDraft((d) => (d === sent ? null : d)) // keep edits typed during the save
    },
  })
  const errors = fieldErrors(save.error)
  const value = (k: Key) => String(draft?.[k] ?? server[k])
  const set = (k: Key, v: unknown) => setDraft((d) => ({ ...d, [k]: v }))
  const field = (k: Key, label: string, more: { unit?: string; help?: string; caution?: string; input: (p: ControlProps) => ReactNode }) => (
    <Field label={label} unit={more.unit} help={more.help} caution={more.caution} errors={errorsAt(errors, [k])} control={more.input} />
  )
  const text = (k: Key, label: string) =>
    field(k, label, { input: (p) => <Input {...p} value={value(k)} onChange={(e) => set(k, e.target.value)} /> })
  // the mask shows as text until edited; a mask sent back, or no edit, leaves the secret as it is
  const secret = (k: SecretKey) =>
    field(k, SECRET_NAMES[k], {
      help: 'Masked. Type to replace it; empty clears it.',
      caution: draft?.[k] === '' && server[k] !== '' ? `Clears the ${SECRET_NAMES[k]} on save.` : undefined,
      input: (p) => (
        // new-password: browsers ignore "off" on a password field and would autofill a saved login
        <Input {...p} type={draft?.[k] === undefined ? 'text' : 'password'} autoComplete="new-password" spellCheck={false} value={value(k)} onFocus={(e) => e.target.select()} onChange={(e) => set(k, e.target.value)} />
      ),
    })
  const num = (k: Key, label: string, more: { unit?: string; help?: string; step: string }) =>
    field(k, label, { ...more, input: (p) => <Input {...p} type="number" step={more.step} value={value(k)} onChange={(e) => set(k, numberValue(e.target.value))} /> })
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (draft) save.mutate(draft)
      }}
    >
      <SaveRow dirty={draft !== null} save={save} errors={errors} note="Saved to the secrets file; applies after a server restart." />
      <Section legend="Connections">
        {text('nt_url', 'NinjaTrader address')}
        {secret('nt_token')}
        {secret('lse_api_key')}
      </Section>
      <Section legend="Backtest and live">
        {num('holdout_months', 'Hold-out', { unit: 'months, most recent; never used for tuning', step: '1' })}
        {num('safety_margin_pct', 'Safety margin', { unit: '% of the daily loss limit or of the drawdown room at session start', help: 'Reaching it halts the account for the day.', step: 'any' })}
        {num('stale_seconds', 'Stale threshold', { unit: 'seconds without an LSE bar in session', help: 'Past it, new entries pause and an alert is raised.', step: '1' })}
      </Section>
    </form>
  )
}

type ChoiceDraft = Record<string, Record<string, unknown>>

function AccountsForm({ server }: { server: Choices }) {
  const qc = useQueryClient()
  const id = useId()
  const [draft, setDraft] = useState<ChoiceDraft | null>(null) // only edited fields: PUT merges
  const save = useMutation({
    mutationFn: (edits: ChoiceDraft) => apiFetch<Choices>('/api/settings/accounts', { method: 'PUT', body: JSON.stringify(edits) }),
    onSuccess: (data, sent) => {
      qc.setQueryData(['settings', 'accounts'], data)
      setDraft((d) => (d === sent ? null : d))
    },
  })
  const errors = fieldErrors(save.error)
  const at = (aid: string, k: 'eval_price' | 'dll_on') => getIn(draft, [aid, k]) ?? server[aid]?.[k]
  const set = (aid: string, k: 'eval_price' | 'dll_on', v: unknown) => setDraft((d) => setIn(d ?? {}, [aid, k], v))
  const ids = Object.keys(server)
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (draft) save.mutate(draft)
      }}
    >
      <SaveRow dirty={draft !== null} save={save} errors={errors} note="Saved to accounts.yaml in the data directory." />
      <p className="mb-2 max-w-[72ch] text-ink-3">Evaluation price: what you paid, after coupons; net per attempt subtracts it.</p>
      <p id={`${id}-dll`} className="mb-4 max-w-[72ch] text-ink-3">
        {DLL_HELP}
      </p>
      {ids.length === 0 ? (
        <p className="text-ink-2">No rule files.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Account</TableHead>
              <TableHead scope="col">Evaluation price</TableHead>
              <TableHead scope="col">Daily loss limit</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ids.map((aid) => {
              const priceErrors = errorsAt(errors, [aid, 'eval_price'])
              const dllErrors = errorsAt(errors, [aid, 'dll_on'])
              const errorId = (k: string) => `${id}-${aid}-${k}-error`
              return (
                <TableRow key={aid}>
                  <TableCell className="value">{aid}</TableCell>
                  <TableCell className="whitespace-normal">
                    <Input
                      aria-label={`${aid} evaluation price`}
                      aria-invalid={priceErrors.length > 0}
                      aria-describedby={priceErrors.length ? errorId('price') : undefined}
                      className="w-32"
                      type="number"
                      step="any"
                      value={String(at(aid, 'eval_price'))}
                      onChange={(e) => set(aid, 'eval_price', numberValue(e.target.value))}
                    />
                    {priceErrors.length > 0 && (
                      <p id={errorId('price')} className="text-red">
                        {priceErrors.join(' ')}
                      </p>
                    )}
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    <Switch
                      aria-label={`${aid} daily loss limit`}
                      aria-invalid={dllErrors.length > 0}
                      aria-describedby={[dllErrors.length > 0 && errorId('dll'), `${id}-dll`].filter(Boolean).join(' ')}
                      checked={at(aid, 'dll_on') === true}
                      onCheckedChange={(c) => set(aid, 'dll_on', c)}
                    />
                    {dllErrors.length > 0 && (
                      <p id={errorId('dll')} className="text-red">
                        {dllErrors.join(' ')}
                      </p>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
    </form>
  )
}

function SettingsPage() {
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => apiFetch<Settings>('/api/settings') })
  const accounts = useQuery({ queryKey: ['settings', 'accounts'], queryFn: () => apiFetch<Choices>('/api/settings/accounts') })
  const state = (q: { isPending: boolean; error: unknown }) => {
    const error = pageError(q.error)
    return error ? <p className="text-red">{error}</p> : q.isPending && <p className="text-ink-3">Loading…</p>
  }
  return (
    <Page title="Settings">
      <div className="grid max-w-[60rem] gap-10">
        <section aria-labelledby="settings-env">
          <h2 id="settings-env" className="mb-4 text-ink">
            Secrets and limits
          </h2>
          {settings.data?.nested_git.length ? (
            <p role="alert" className="mb-6 max-w-[72ch] border border-ember p-4 text-ember">
              The terminal left a .git in the repo: {settings.data.nested_git.join(', ')}. Git, a git-aware prompt or an editor on this machine would run its config there. Delete it before running git; from the repo root, <code>find data rules strategies -name .git</code> must print nothing.
            </p>
          ) : null}
          {settings.data ? <SettingsForm server={settings.data} /> : state(settings)}
        </section>
        <section aria-labelledby="settings-accounts">
          <h2 id="settings-accounts" className="mb-4 text-ink">
            Accounts
          </h2>
          {accounts.data ? <AccountsForm server={accounts.data} /> : state(accounts)}
        </section>
      </div>
    </Page>
  )
}

export const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/settings',
  component: SettingsPage,
})
