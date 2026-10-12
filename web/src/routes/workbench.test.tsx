import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { routeTree } from '@/router'
import { type Latest, closeLine, inputFrame, latestPollMs, resizeFrame } from './workbench'

describe('terminal frames', () => {
  it('encodes input and resize as the server parses them', () => {
    expect(JSON.parse(inputFrame('ls "a"\r'))).toEqual({ type: 'input', data: 'ls "a"\r' })
    expect(JSON.parse(resizeFrame(120, 40))).toEqual({ type: 'resize', cols: 120, rows: 40 })
  })
})

describe('closeLine', () => {
  it('says the shell exited on a normal close', () => {
    expect(closeLine(1000, '')).toBe('Disconnected: the shell exited.')
  })

  it('names a refused or lost connection in plain words', () => {
    expect(closeLine(1006, '')).toMatch(/^Disconnected: the connection closed or was refused .*Reload to start a new shell\.$/)
  })

  it('gives the server reason, else the code', () => {
    expect(closeLine(1011, 'shell did not start')).toBe('Disconnected: shell did not start.')
    expect(closeLine(1009, 'too much input pending')).toBe('Disconnected: too much input pending.')
    expect(closeLine(4000, '')).toBe('Disconnected: code 4000.')
  })
})

const period = (over = {}) => ({ p_pass_and_payout: 0.19, pass_rate: 0.31, expected_net_per_attempt: -42.5, microscalp_flags: 0, ...over })
const latest = (over: Partial<Latest> = {}): Latest => ({
  key: 'a'.repeat(64),
  created_at: '2026-10-10T14:00:00Z',
  strategy: 'orb',
  account: 'lucid-flex-50k',
  contract: 'MNQ',
  periods: { tuning: period(), holdout: null },
  holdout_gap: false,
  ...over,
})

async function workbench(results: Latest[]): Promise<string> {
  const client = new QueryClient()
  client.setQueryData(['backtests', 'results'], results)
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/'] }) })
  await router.load()
  return renderToString(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('latest backtest pane', () => {
  it('shows the newest report with its figures and a link to the full report', async () => {
    const html = await workbench([latest(), latest({ key: 'b'.repeat(64), strategy: 'older' })])
    expect(html).toContain('orb')
    expect(html).not.toContain('older')
    expect(html).toContain('19.0%')
    expect(html).toContain('-$42.50')
    expect(html).toContain(`href="/backtests/${'a'.repeat(64)}"`)
  })

  it('says when there is no report yet', async () => {
    expect(await workbench([])).toContain('No backtest yet.')
  })

  it('flags microscalps and the hold-out gap in Ember', async () => {
    const html = await workbench([latest({ periods: { tuning: period({ microscalp_flags: 3 }), holdout: period() }, holdout_gap: true })])
    expect(html).toContain('text-ember">3<')
    expect(html).toContain('likely overfit')
  })
})

describe('latestPollMs', () => {
  it('polls every 3 s, stopping only on a 401', () => {
    expect(latestPollMs({ error: null })).toBe(3000)
    expect(latestPollMs({ error: new ApiError(500, '') })).toBe(3000)
    expect(latestPollMs({ error: new ApiError(401, '') })).toBe(false)
  })
})
