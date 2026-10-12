import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { routeTree } from '@/router'
import { type RuleRow, freshness } from './rules'

const row = (r: Partial<RuleRow>): RuleRow => ({
  id: 'lucid-flex-50k',
  verified_on: '2026-10-01',
  age_days: 10,
  stale: false,
  error: null,
  ...r,
})

describe('freshness', () => {
  it.each([
    [row({}), 'normal', 'Verified 10 days ago'],
    [row({ age_days: 1 }), 'normal', 'Verified 1 day ago'],
    [row({ age_days: 0 }), 'normal', 'Verified today'],
    [row({ age_days: 31, stale: true }), 'caution', 'Verified 31 days ago'],
    [row({ verified_on: '2026-10-12', age_days: -1, stale: true }), 'caution', 'Verified in the future'],
    [row({ verified_on: null, age_days: null, stale: true }), 'caution', 'Never verified'],
    [row({ verified_on: null, age_days: null, stale: null, error: 'bad yaml' }), 'alarm', 'Invalid file'],
  ] as const)('%#: %s', (r, tone, text) => {
    expect(freshness(r)).toEqual({ tone, text })
  })
})

describe('rules list', () => {
  it('lists each file with its status, error and an Edit link, marking the selected one', async () => {
    const client = new QueryClient()
    client.setQueryData<RuleRow[]>(['rules'], [row({}), row({ id: 'lucid-pro-25k', verified_on: null, age_days: null, stale: null, error: 'bad yaml' })])
    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/rules?account=lucid-pro-25k'] }) })
    await router.load()
    const html = renderToString(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(html).toContain('Verified 10 days ago')
    expect(html).not.toContain('>null<')  // a successful query renders no error line
    expect(html).toMatch(/text-red[^"]*">Invalid file</)
    expect(html).toContain('bad yaml')
    expect(html).toMatch(/aria-label="Edit lucid-flex-50k"[^>]*href="\/rules\?account=lucid-flex-50k"/)
    expect([...html.matchAll(/data-state="selected"[^>]*><td[^>]*>([^<]+)</g)].map((m) => m[1])).toEqual(['lucid-pro-25k'])
  })
})

// lucid-daily-100k as GET /api/rules/{id} returns it, with funded locks_at and consistency null.
const rules = {
  firm: 'lucid',
  account: 'daily-100k',
  verified_on: '2026-10-11',
  source_url: 'https://support.lucidtrading.com/en/collections/19692910-luciddaily',
  automation_allowed: true,
  evaluation: {
    max_drawdown: { amount: 3000, trailing: 'intraday', locks_at: 3100 },
    daily_loss_limit: 1800,
    consistency: { max_day_pct: 50 },
    min_trading_days: 0,
    profit_target: 6000,
  },
  funded: {
    max_drawdown: { amount: 3000, trailing: 'intraday', locks_at: null },
    daily_loss_limit: 1800,
    consistency: null,
    min_trading_days: 0,
    payout: { max_pct_of_profit: 13.88, split_pct: 90, min_days: 0, min_profit: 500, min_day_profit: 0, max_amount: null, max_payouts: null },
    scaling: null,
  },
  session: { flat_by: '16:45', tz: 'America/New_York' },
  contracts: { max_minis: 6, micros_per_mini: 10 },
  prohibited: { microscalp: { max_profit_pct: 50, hold_seconds: 5 }, hft: true },
  costs: { activation_fee: 0, commission_per_side: { ES: 1.75, MES: 0.5, NQ: 1.75, MNQ: 0.5 } },
}

async function renderEditor(id: string, detail: object) {
  const client = new QueryClient()
  client.setQueryData<RuleRow[]>(['rules'], [row({ id })])
  client.setQueryData(['rules', id], { ...row({ id }), ...detail })
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [`/rules?account=${id}`] }) })
  await router.load()
  return renderToString(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

/** The <input> labelled `label`, found through the label's for= id. */
function input(html: string, label: string): string {
  const id = html.match(new RegExp(`<label for="([^"]+)"[^>]*>${label}</label>`))?.[1]
  return html.match(new RegExp(`<input[^>]*id="${id}"[^>]*>`))?.[0] ?? `no input labelled ${label}`
}

describe('rules editor', () => {
  it('shows the selected file field by field, nulls as None, and Save disabled until an edit', async () => {
    const html = await renderEditor('lucid-daily-100k', { rules })
    expect(html).toContain('aria-label="Edit lucid-daily-100k"')
    expect([...html.matchAll(/<legend[^>]*>([^<]+)</g)].map((m) => m[1])).toEqual(['Account', 'Evaluation', 'Funded', 'Payout', 'Scaling', 'Session', 'Contracts', 'Prohibited', 'Costs'])
    expect(input(html, 'Verified on')).toMatch(/type="date"[^>]*value="2026-10-11"/)
    expect(input(html, 'Profit target')).toContain('value="6000"')
    expect(input(html, 'Max % of profit')).toContain('value="13.88"')
    expect(input(html, 'MNQ commission per side')).toContain('value="0.5"')
    expect(input(html, 'Time zone')).toContain('value="America/New_York"')
    // evaluation locks_at is set, funded locks_at is null: None unchecked, then checked
    expect([...html.matchAll(/aria-checked="(\w+)"[^>]*aria-label="Drawdown locks at: none"/g)].map((m) => m[1])).toEqual(['false', 'true'])
    expect(html).toMatch(/<button[^>]*type="submit"[^>]*disabled=""[^>]*>Save<\/button>/)
    // funded.consistency is null: only the evaluation's field shows
    expect(html.match(/Max day % of profit/g)).toHaveLength(1)
    // payout extras; scaling null: "No scaling" checked, no tiers
    expect(input(html, 'Min day profit')).toContain('value="0"')
    expect(html).toMatch(/aria-checked="true"[^>]*aria-label="Max per payout: none"/)
    expect(html).toMatch(/aria-checked="true"[^>]*aria-label="Max payouts: none"/)
    expect(html).toMatch(/aria-checked="true"[^>]*id="([^"]+)"[\s\S]*<label for="\1"[^>]*>No scaling/)
    expect(html).not.toContain('Tier 1')
  })

  it('shows each scaling tier with its own remove button and an Add tier button', async () => {
    const scaling = [{ from_profit: 0, max_minis: 4 }, { from_profit: 1000, max_minis: 6 }]
    const payout = { ...rules.funded.payout, min_day_profit: 150, max_amount: 2000, max_payouts: 5 }
    const html = await renderEditor('lucid-daily-100k', { rules: { ...rules, funded: { ...rules.funded, payout, scaling } } })
    expect(input(html, 'Tier 2 from profit')).toContain('value="1000"')
    expect(input(html, 'Tier 2 max minis')).toContain('value="6"')
    expect(input(html, 'Max per payout')).toContain('value="2000"')
    expect(input(html, 'Max payouts')).toContain('value="5"')
    expect(html).toMatch(/<button(?![^>]*disabled="")[^>]*>Remove tier 2<\/button>/)
    expect(html).toContain('>Add tier</button>')
  })

  it('disables Remove on the last tier: "No scaling" removes it', async () => {
    const funded = { ...rules.funded, scaling: [{ from_profit: 0, max_minis: 6 }] }
    const html = await renderEditor('lucid-daily-100k', { rules: { ...rules, funded } })
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Remove tier 1<\/button>/)
  })

  it('asks to fix a file that does not validate by hand, with no form', async () => {
    const html = await renderEditor('lucid-pro-25k', { error: 'lucid/pro-25k.yaml: bad yaml', rules: null })
    expect(html).toContain('Fix this file by hand')
    expect(html).toContain('lucid/pro-25k.yaml: bad yaml')
    expect(html).not.toContain('<form')
  })
})
