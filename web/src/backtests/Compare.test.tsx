import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToStaticMarkup, renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { routeTree } from '@/router'
import { type CompareRow, CompareView, type Group, type Period, compareKey, figures, identity, money, pct } from './Compare'

const period = (over: Partial<Period> = {}): Period => ({
  p_pass_and_payout: 0.19,
  pass_rate: 0.31,
  expected_net_per_attempt: 87.5,
  microscalp_flags: 0,
  ...over,
})
const row = (over: Partial<CompareRow>): CompareRow => ({
  key: 'k',
  strategy: 'orb',
  account: 'lucid-flex-50k',
  contract: 'MNQ',
  periods: { tuning: period(), holdout: period({ p_pass_and_payout: 0.05 }) },
  holdout_gap: false,
  ...over,
})

const view = (groups: Group[]) => renderToStaticMarkup(<CompareView view="account" groups={groups} pending={false} error={null} />)

describe('helpers', () => {
  it('drops the viewed column from the identity columns', () => {
    expect(identity('account')).toEqual(['strategy', 'contract'])
    expect(identity('strategy')).toEqual(['account', 'contract'])
  })

  it('formats rates as percent and net with a sign', () => {
    expect(pct(0.19)).toBe('19.0%')
    expect(money(87.5)).toBe('+$87.50')
    expect(money(-12)).toBe('-$12.00')
    expect(money(0)).toBe('$0.00')
  })

  it('gives a period its figures, and dashes for a missing hold-out', () => {
    expect(figures(period({ microscalp_flags: 4 }))).toEqual(['19.0%', '31.0%', '+$87.50', '4'])
    expect(figures(null)).toEqual(['—', '—', '—', '—'])
  })

  it('shares the compare prefix the jobs socket invalidates', () => {
    expect(compareKey('strategy').slice(0, 2)).toEqual(['backtests', 'compare'])
  })
})

describe('CompareView', () => {
  it('keeps groups and rows in server order, even when tuning figures look unsorted', () => {
    const html = view([
      { group: 'zeta-acct', rows: [row({ key: 'a', strategy: 'second', periods: { tuning: period({ p_pass_and_payout: 0.1 }), holdout: null } }), row({ key: 'b', strategy: 'first' })] },
      { group: 'alpha-acct', rows: [row({ key: 'c', strategy: 'third' })] },
    ])
    const order = ['zeta-acct', '>second<', '>first<', 'alpha-acct', '>third<'].map((s) => html.indexOf(s))
    expect(order.every((i) => i >= 0)).toBe(true)
    expect(order).toEqual([...order].sort((a, b) => a - b))
  })

  it('mutes hold-out figures and marks microscalp flags and the hold-out gap Ember', () => {
    const html = view([
      {
        group: 'lucid-flex-50k',
        rows: [row({ periods: { tuning: period({ microscalp_flags: 3 }), holdout: period({ p_pass_and_payout: 0.05, microscalp_flags: 0 }) }, holdout_gap: true })],
      },
    ])
    expect(html).toContain('class="p-2 align-middle whitespace-nowrap [&amp;:has([role=checkbox])]:pr-0 [&amp;&gt;[role=checkbox]]:translate-y-[2px] value text-right text-ember">3</td>')
    expect(html).toContain('value text-right text-ink-3">5.0%</td>')
    expect(html).toContain('value text-right text-ink-3">0</td>')
    expect(html).toContain('value text-right">19.0%</td>')
    expect(html).toMatch(/<span class="[^"]*text-ember">Gap<\/span>/)
  })

  it('shows no gap when there is none, and dashes for a tuning-only report', () => {
    const html = view([{ group: 'g', rows: [row({ periods: { tuning: period(), holdout: null } })] }])
    expect(html).not.toContain('Gap</span>')
    expect([...html.matchAll(/text-ink-3">—<\/td>/g)]).toHaveLength(4)
  })

  it('says when there are no results, while loading and on a failed GET', () => {
    expect(view([])).toContain('No results yet.')
    expect(renderToStaticMarkup(<CompareView view="account" pending error={null} />)).toContain('Loading…')
    expect(renderToStaticMarkup(<CompareView view="account" pending={false} error="500: boom" />)).toContain('<p role="alert" class="text-red">500: boom</p>')
  })
})

describe('Backtests page', () => {
  it('shows the compare tabs, by account first', async () => {
    const client = new QueryClient()
    client.setQueryData(['backtests', 'jobs'], [])
    client.setQueryData<Group[]>(compareKey('account'), [{ group: 'lucid-flex-50k', rows: [row({ strategy: 'orb-fast' })] }])
    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/backtests'] }) })
    await router.load()
    const html = renderToString(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(html).toContain('<h2 id="compare">Compare</h2>')
    expect(html).toMatch(/role="tab" aria-selected="true"[^>]*>By account</)
    expect(html).toMatch(/role="tab" aria-selected="false"[^>]*>By strategy</)
    expect(html).toContain('>orb-fast</td>')
  })
})
