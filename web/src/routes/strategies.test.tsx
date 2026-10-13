import { readFileSync } from 'node:fs'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToStaticMarkup, renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { SESSION_EXPIRED } from '@/errorText'
import { routeTree } from '@/router'
import {
  type Armed,
  type NtAccount,
  type Strategy,
  type Summary,
  EMPTY_DRAFT,
  REFUSALS,
  SummaryFacts,
  armBody,
  armErrorText,
  armedAt,
  confirmBody,
  needsConfirmation,
  refusal,
  targets,
} from './strategies'

const summary: Summary = {
  start: '2019-01-02',
  end: '2025-10-09',
  attempts: 1700,
  pass_rate: 0.31,
  p_pass_and_payout: 0.19,
  expected_gross_per_attempt: 187.5,
  expected_net_per_attempt: 90,
  holdout_gap: false,
  strategy_hash: 'a'.repeat(64),
  rules_hash: 'b'.repeat(64),
  dll_on: true,
  liquidation_level: 48500,
}

const strategy: Strategy = {
  name: 'orb',
  path: '/repo/strategies/orb.py',
  hash: 'a'.repeat(64),
  market: 'NQ',
  params: { or_minutes: 15, side: 'long' },
  error: null,
  reports: [
    { key: 'k2', account: 'lucid-flex-50k', contract: 'MNQ', created_at: '2026-10-12T10:00:00Z', periods: { tuning: { p_pass_and_payout: 0.19 } } },
    { key: 'k1', account: 'lucid-flex-50k', contract: 'MNQ', created_at: '2026-10-11T10:00:00Z', periods: { tuning: { p_pass_and_payout: 0.12 } } },
  ],
}
const broken: Strategy = { name: 'bad', path: '/repo/strategies/bad.py', hash: null, market: null, params: {}, error: 'SyntaxError: invalid syntax', reports: [] }
const armed: Armed = {
  account: 'LFE0507',
  strategy_path: '/repo/strategies/orb.py',
  strategy_hash: 'a'.repeat(64),
  rules_account: 'lucid-flex-50k',
  contract: 'MNQ',
  mode: 'dry',
  phase: 'evaluation',
  armed_at: '2026-10-13T14:05:09.123+00:00',
  dll_on: true,
  rules_hash: 'b'.repeat(64),
  instruments_hash: 'c'.repeat(64),
  start_balance: null,
  peak_balance: null,
}
const nt: NtAccount[] = [{ id: 'LFE0507', name: 'LFE0507', is_sim: false, balance: 50250 }]

const conflict = (body: unknown, status = 409) => new ApiError(status, JSON.stringify(body))

describe('arming helpers', () => {
  it('offers each account type and contract once, newest first', () => {
    expect(targets([...strategy.reports, { account: 'lucid-pro-50k', contract: 'NQ' }])).toEqual([
      { account: 'lucid-flex-50k', contract: 'MNQ' },
      { account: 'lucid-pro-50k', contract: 'NQ' },
    ])
  })

  it('sends no balances for a dry run, and both as numbers otherwise', () => {
    const draft = { ...EMPTY_DRAFT, strategy: strategy.path, account: 'LFE0507', start: '50000', peak: '50250' }
    const target = { account: 'lucid-flex-50k', contract: 'MNQ' }
    expect(armBody(draft, target)).toEqual({
      account: 'LFE0507',
      strategy_path: strategy.path,
      rules_account: 'lucid-flex-50k',
      contract: 'MNQ',
      mode: 'dry',
      phase: 'evaluation',
      confirm: false,
    })
    expect(armBody({ ...draft, mode: 'live', phase: 'funded' }, target)).toMatchObject({ mode: 'live', phase: 'funded', start_balance: 50000, peak_balance: 50250 })
    // an emptied balance is sent as '', so the server names the field
    expect(armBody({ ...draft, mode: 'sim', start: '' }, target)).toMatchObject({ start_balance: '' })
  })

  it('confirms the same body, echoing the hashes and dll_on the summary showed', () => {
    const body = armBody({ ...EMPTY_DRAFT, strategy: strategy.path, account: 'LFE0507', mode: 'live', start: '50000', peak: '50000' }, { account: 'lucid-flex-50k', contract: 'MNQ' })
    expect(confirmBody(body, summary)).toEqual({ ...body, confirm: true, strategy_hash: summary.strategy_hash, rules_hash: summary.rules_hash, dll_on: true })
  })

  it('reads a refusal and its summary from the error body', () => {
    expect(refusal(conflict({ error: 'needs_confirmation', summary }))).toEqual({ error: 'needs_confirmation', summary })
    expect(needsConfirmation(conflict({ error: 'needs_confirmation', summary }))).toEqual(summary)
    expect(needsConfirmation(conflict({ error: 'no_backtest', summary: null }))).toBeNull()
    expect(refusal(conflict({ detail: 'x' }, 400))).toBeNull()
    expect(refusal(new ApiError(500, 'not json'))).toBeNull()
    expect(refusal(new Error('x'))).toBeNull()
  })

  it('puts a refusal in words and leaves other errors to errorText', () => {
    expect(armErrorText(conflict({ error: 'rules_changed', summary: null }))).toBe('409: The rules file changed since its backtests. Run the backtest again.')
    expect(armErrorText(conflict({ error: 'nt_unavailable', summary: null }, 502))).toMatch(/^502: NinjaTrader is unavailable/)
    expect(armErrorText(conflict({ error: 'something_new', summary: null }))).toBe('409: {"error":"something_new","summary":null}')
    expect(armErrorText(conflict({ detail: [{ msg: 'Value error, peak_balance is below start_balance' }] }, 422))).toBe('422: Value error, peak_balance is below start_balance')
    expect(armErrorText(new ApiError(401, ''))).toBe(SESSION_EXPIRED)
  })

  it('has words for every refusal code in the arming contract', () => {
    const contract = readFileSync(new URL('../../../docs/contracts.md', import.meta.url), 'utf8')
    const conflicts = contract.match(/409 \{error: ((?:"[a-z_]+"\|?)+)/)?.[1]?.match(/[a-z_]+/g) ?? []
    const unavailable = contract.match(/502 \{error: "([a-z_]+)"\}/)?.[1]
    expect(conflicts).toContain('no_backtest') // the contract line was found
    // needs_confirmation opens the dialog instead
    expect(Object.keys(REFUSALS).sort()).toEqual([...conflicts.filter((c) => c !== 'needs_confirmation'), unavailable].sort())
  })

  it('shows armed_at to the minute in UTC', () => {
    expect(armedAt(armed.armed_at)).toBe('2026-10-13 14:05 UTC')
  })
})

describe('SummaryFacts', () => {
  it('shows the tuning summary, the balances with the liquidation level, dll_on and the hashes', () => {
    const html = renderToStaticMarkup(<SummaryFacts summary={summary} start={50000} peak={50250} />)
    for (const text of ['2019-01-02 to 2025-10-09', '1,700', '31.0%', '19.0%', '+$187.50', '+$90.00', '$50,000.00', '$50,250.00', '$48,500.00', 'On'])
      expect(html).toContain(text)
    // the first 12 characters, the full hash one tap or key press away (not a title tooltip)
    expect(html).toContain(`<details><summary class="cursor-pointer">bbbbbbbbbbbb…</summary><span class="break-all select-all">${summary.rules_hash}</span></details>`)
    expect(html).toContain(`>${summary.strategy_hash}<`)
    expect(html).not.toContain('title=')
    expect(html).toContain('<dd class="value">None</dd>')
    expect(html).not.toContain('text-ember')
  })

  it('marks a hold-out gap Ember', () => {
    const html = renderToStaticMarkup(<SummaryFacts summary={{ ...summary, holdout_gap: true, dll_on: false }} />)
    expect(html).toContain('<dd class="text-ember">Yes. Hold-out P(pass and payout) is under half of tuning&#x27;s: likely overfit.</dd>')
    expect(html).toContain('<dd class="value">Off</dd>')
  })
})

async function render(data: { strategies?: Strategy[]; armed?: Armed[]; nt?: NtAccount[] }) {
  const client = new QueryClient()
  if (data.strategies) client.setQueryData(['strategies'], data.strategies)
  if (data.armed) client.setQueryData(['arming'], data.armed)
  if (data.nt) client.setQueryData(['arming', 'accounts'], data.nt)
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/strategies'] }) })
  await router.load()
  return renderToString(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('strategies page', () => {
  it('lists armed accounts with a Disarm each, and strategy files with params, errors and the latest backtest', async () => {
    const html = await render({ strategies: [strategy, broken], armed: [armed], nt })
    expect([...html.matchAll(/<h2[^>]*>([^<]+)</g)].map((m) => m[1])).toEqual(['Armed accounts', 'Arm', 'Strategy files'])
    expect(html).toContain('2026-10-13 14:05 UTC')
    expect(html).toMatch(/text-ink-3">Dry run</)
    expect(html).toContain('aria-label="Disarm LFE0507"')
    expect(html).toContain('or_minutes=15 side=long')
    expect(html).toContain('<p class="max-w-[72ch] text-red wrap-anywhere">SyntaxError: invalid syntax</p>')
    expect(html).toMatch(/2 · latest <a[^>]*href="\/backtests\/k2"[^>]*>lucid-flex-50k MNQ<\/a> 19.0%/)
    // nothing chosen yet: the arm button is off, a dry run by default
    expect(html).toMatch(/<button[^>]*type="submit"[^>]*disabled=""[^>]*>Arm dry run<\/button>/)
  })

  it('says when nothing is armed and while the lists load', async () => {
    const html = await render({ armed: [] })
    expect(html).toContain('Nothing armed.')
    expect(html).toContain('Loading…')
  })
})
