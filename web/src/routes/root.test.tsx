import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { SESSION_EXPIRED } from '@/errorText'
import { routeTree } from '@/router'
import { sessionExpired } from './root'

async function render(path: string, client = new QueryClient()): Promise<string> {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) })
  await router.load()
  return renderToString(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('shell', () => {
  it.each([
    ['/', 'Workbench'],
    ['/rules', 'Rules'],
    ['/data', 'Data'],
    ['/backtests', 'Backtests'],
    ['/backtests/abc123', 'Report'],
    ['/strategies', 'Strategies'],
    ['/live', 'Live'],
    ['/settings', 'Settings'],
    ['/no-such-page', 'Not found'],
  ])('renders %s inside the rail shell', async (path, title) => {
    const html = await render(path)
    expect(html).toContain('<nav aria-label="Pages">')
    expect(html).toContain(`<h1>${title}</h1>`)
  })

  it('marks only the matching page current, Backtests for a report', async () => {
    const current = (html: string) => [...html.matchAll(/aria-current="page"[^>]*>([^<]+)</g)].map((m) => m[1])
    expect(current(await render('/'))).toEqual(['Workbench'])
    expect(current(await render('/backtests/abc123'))).toEqual(['Backtests'])
  })

  it('shows the report key in the strip', async () => {
    expect(await render('/backtests/abc123')).toContain('<span class="value min-w-0 break-all">abc123</span>')
  })
})

describe('session expired', () => {
  const failWith = async (client: QueryClient, status: number) =>
    client.prefetchQuery({ queryKey: ['x'], queryFn: () => Promise.reject(new ApiError(status, '')) })

  it('is flagged by a 401 query error only', async () => {
    const client = new QueryClient()
    expect(sessionExpired(client)).toBe(false)
    await failWith(client, 500)
    expect(sessionExpired(client)).toBe(false)
    await failWith(client, 401)
    expect(sessionExpired(client)).toBe(true)
  })

  it('shows the session-expired message in the shell', async () => {
    const client = new QueryClient()
    await failWith(client, 401)
    expect(await render('/rules', client)).toContain(SESSION_EXPIRED)
  })
})
