import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { account, desk, status } from '@/live/fixtures'
import type { LiveStatus } from '@/live/model'
import { routeTree } from '@/router'
import { statusPollMs } from './live'

async function render(s?: LiveStatus, client = new QueryClient(), path = '/live') {
  if (s) client.setQueryData(['live', 'status'], s)
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) })
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
      // nor the focus field, sleeves or tape from it
      expect(html).not.toContain('aria-label="Focused account"')
      expect(html).not.toContain('Journal · today')
      expect(html).not.toContain('LFE 0911')
    }
  })

  it('focuses the most severe account, with Resolve, and files the others as sleeves', async () => {
    const html = await render(desk())
    const focus = html.match(/<section aria-label="Focused account".*?<\/section>/s)?.[0] ?? ''
    expect(focus).toMatch(/<h2[^>]*>LFE 0911<\/h2>/)
    expect(focus).toContain('aria-label="Risk normal, book alarm"')
    expect(focus).toContain('aria-label="Resolve LFE 0911"')
    for (const other of ['LFE 0507', 'LPR 1123', 'LDA 0042']) expect(html).toContain(`<article aria-label="${other}"`)
    expect(html).not.toContain('<article aria-label="LFE 0911"')
  })

  it('draws the dial trace with the eased animation, off under reduced motion', async () => {
    const html = await render(desk())
    const trace = html.match(/<path data-trace="live"[^>]*>/)?.[0] ?? ''
    expect(trace).toContain('pathLength="1"')
    expect(trace).toContain('animate-trace')
    expect(trace).toContain('motion-reduce:animate-none')
  })

  it('marks the meter safety cell with the inset safety-mark utility, never an arbitrary shadow', async () => {
    const html = await render(desk())
    const mark = html.match(/<i [^>]*data-mark="true"[^>]*>/)?.[0] ?? ''
    expect(mark).toContain('safety-mark')
    expect(mark).not.toContain('shadow-[')
  })

  it("ends the journal tape with a Full journal link to today's journal", async () => {
    const html = await render(desk())
    expect(html).toMatch(/<a [^>]*href="\/live\/journal"[^>]*>Full journal<\/a>/)
    const page = await render(desk(), new QueryClient(), '/live/journal')
    expect(page).toContain('<h1>Journal</h1>')
    expect(page).toContain('Trading day')
  })

  it('polls every 2 s through errors, but not after a 401', () => {
    expect(statusPollMs({ error: null })).toBe(2000)
    expect(statusPollMs({ error: new ApiError(500, '') })).toBe(2000)
    expect(statusPollMs({ error: new ApiError(401, '') })).toBe(false)
  })
})
