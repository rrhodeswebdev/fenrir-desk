import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { account, status } from '@/live/fixtures'
import type { LiveStatus } from '@/live/model'
import { routeTree } from '@/router'
import { statusPollMs } from './live'

async function render(s?: LiveStatus, client = new QueryClient()) {
  if (s) client.setQueryData(['live', 'status'], s)
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/live'] }) })
  await router.load()
  return renderToString(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('live page', () => {
  it('shows the clock, the connections and the system strip with the alarming account beside it in Red', async () => {
    const book = account({ account: 'LFE 0911', halted: true, halt_kind: 'reconcile' })
    const html = await render(status({ nt: { connected: false, last_heartbeat: null }, accounts: [account(), book] }))
    expect(html).toContain('<span class="label">New York</span>')
    expect(html).toContain('role="group" aria-label="Connections"')
    expect(html).toContain('<span class="value">14:31:00</span>') // the last bar, New York time
    expect(html).toContain('role="img" aria-label="Feed normal, broker alarm, drift normal"')
    expect(html).toContain('<span class="label text-red">Book · LFE 0911</span>')
    expect(html).toMatch(/<button[^>]*data-variant="destructive"[^>]*>Kill all<\/button>/)
  })

  it('names no account when none is in alarm, and says when nothing is armed', async () => {
    const html = await render(status({ accounts: [] }))
    expect(html).not.toContain('text-red')
    expect(html).toContain('aria-label="Feed idle, broker idle, drift normal"')
    expect(html).toContain('No account is armed.')
  })

  it('offers the kill switch before the status has loaded', async () => {
    const html = await render()
    expect(html).toContain('Loading…')
    const button = html.match(/<button[^>]*>Kill all<\/button>/)?.[0]
    expect(button).toBeDefined()
    expect(button).not.toContain('disabled=""')
  })

  it('never shows the last status as current once a read failed (500 or 401)', async () => {
    for (const error of [new ApiError(500, 'boom'), new ApiError(401, '')]) {
      const client = new QueryClient()
      client.setQueryData(['live', 'status'], status({ accounts: [account({ account: 'LFE 0911', halted: true, halt_kind: 'reconcile' })] }))
      client.getQueryCache().find({ queryKey: ['live', 'status'] })!.setState({ status: 'error', error })
      const html = await render(undefined, client)
      expect(html).toContain('Status unavailable')
      expect(html).not.toContain('aria-label="Feed')
      expect(html).not.toContain('Book · LFE 0911')
    }
  })

  it('polls every 2 s through errors, but not after a 401', () => {
    expect(statusPollMs({ error: null })).toBe(2000)
    expect(statusPollMs({ error: new ApiError(500, '') })).toBe(2000)
    expect(statusPollMs({ error: new ApiError(401, '') })).toBe(false)
  })
})
