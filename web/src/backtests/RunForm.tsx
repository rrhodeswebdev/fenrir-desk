import { useMutation, useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { apiFetch } from '@/api'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { errorText, pageError } from '@/errorText'

// GET /api/backtests/options; POST /api/backtests/jobs (docs/contracts.md "Plan 14 — backtest jobs API")
export type Options = { strategies: string[]; accounts: string[]; contracts: string[] }
/** The ticked values on each axis, in the order they were ticked. */
export type Picks = Options
export type Axis = keyof Picks

export const noPicks: Picks = { strategies: [], accounts: [], contracts: [] }

export const toggle = (picks: Picks, axis: Axis, value: string): Picks => ({
  ...picks,
  [axis]: picks[axis].includes(value) ? picks[axis].filter((v) => v !== value) : [...picks[axis], value],
})

/** No contract ticked sends null: every contract of each strategy's market. */
export const jobBody = ({ strategies, accounts, contracts }: Picks) => ({
  strategies,
  accounts,
  contracts: contracts.length ? contracts : null,
})

export const canRun = (picks: Picks): boolean => picks.strategies.length > 0 && picks.accounts.length > 0

export const postJob = (picks: Picks) =>
  apiFetch<{ job_id: string }>('/api/backtests/jobs', { method: 'POST', body: JSON.stringify(jobBody(picks)) })

const axes: [Axis, string][] = [
  ['strategies', 'Strategies'],
  ['accounts', 'Accounts'],
  ['contracts', 'Contracts'],
]

export function RunForm() {
  const options = useQuery({
    queryKey: ['backtests', 'options'],
    queryFn: () => apiFetch<Options>('/api/backtests/options'),
  })
  const [picks, setPicks] = useState(noPicks)
  const run = useMutation({ mutationFn: postJob })
  const message = pageError(options.error)
  if (!options.data)
    return (
      <>
        {message && (
          <p role="alert" className="text-red">
            {message}
          </p>
        )}
        {options.isPending && <p className="text-ink-3">Loading…</p>}
      </>
    )
  return (
    <RunFormView
      options={options.data}
      picks={picks}
      onToggle={(axis, value) => setPicks((p) => toggle(p, axis, value))}
      onRun={() => run.mutate(picks)}
      pending={run.isPending}
      error={run.error}
      jobId={run.data?.job_id}
    />
  )
}

export function RunFormView({
  options,
  picks,
  onToggle,
  onRun,
  pending,
  error,
  jobId,
}: {
  options: Options
  picks: Picks
  onToggle: (axis: Axis, value: string) => void
  onRun: () => void
  pending: boolean
  error: unknown
  jobId?: string
}) {
  return (
    <form
      aria-labelledby="run"
      className="grid gap-6"
      onSubmit={(e) => {
        e.preventDefault()
        onRun()
      }}
    >
      <h2 id="run">Run · strategies × accounts × contracts</h2>
      <div className="grid gap-8 md:grid-cols-3">
        {axes.map(([axis, legend]) => (
          <Picker key={axis} legend={legend} values={options[axis]} picked={picks[axis]} onToggle={(v) => onToggle(axis, v)} />
        ))}
      </div>
      <p className="text-ink-3">No contract ticked runs every contract of each strategy's market.</p>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <Button type="submit" disabled={!canRun(picks) || pending}>
          {pending ? 'Queuing' : 'Run'}
        </Button>
        {jobId && (
          <span role="status" className="flex items-baseline gap-2">
            <span className="label">Queued job</span>
            <span className="value">{jobId}</span>
          </span>
        )}
      </div>
      {error != null && (
        <p role="alert" className="text-red">
          {errorText(error)}
        </p>
      )}
    </form>
  )
}

function Picker({ legend, values, picked, onToggle }: { legend: string; values: string[]; picked: string[]; onToggle: (v: string) => void }) {
  const id = useId()
  return (
    <fieldset className="grid content-start gap-3">
      <legend className="label mb-3">{legend}</legend>
      {values.length === 0 && <p className="text-ink-3">None</p>}
      {values.map((v, i) => (
        <div key={v} className="flex items-center gap-3">
          <Checkbox id={`${id}-${i}`} checked={picked.includes(v)} onCheckedChange={() => onToggle(v)} />
          <Label htmlFor={`${id}-${i}`} className="value">
            {v}
          </Label>
        </div>
      ))}
    </fieldset>
  )
}
