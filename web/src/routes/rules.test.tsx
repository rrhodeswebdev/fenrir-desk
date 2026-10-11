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
