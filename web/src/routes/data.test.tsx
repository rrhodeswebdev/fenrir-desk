import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToStaticMarkup, renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { routeTree } from '@/router'
import { type Overview, PullView } from './data'

const pull = (state: Overview['pull']['state']): Overview['pull'] => ({
  state,
  started_at: state === 'idle' ? null : '2026-10-11T18:02:03Z',
  finished_at: null,
  returncode: null,
  log_tail: [],
})

const overview = (over: Partial<Overview> = {}): Overview => ({
  series: [{ series: 'ES', first_day: '2016-06-01', last_day: '2026-10-09', days: 2509, gaps: [] }],
  rolls: [
    { series: 'ES', start: '2026-01-01', contract: 'ESH26' },
    { series: 'ES', start: '2026-09-10', contract: 'ESZ26' },
  ],
  front: { ES: 'ESZ26' },
  pull: pull('idle'),
  ...over,
})

async function render(data: Overview, error?: ApiError): Promise<string> {
  const client = new QueryClient()
  client.setQueryData(['data'], data)
  // a failed refetch: TanStack keeps the last data beside the error
  if (error) client.getQueryCache().find({ queryKey: ['data'] })?.setState({ status: 'error', error })
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/data'] }) })
  await router.load()
  return renderToString(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

describe('Data page', () => {
  it('disables the control and reads Pulling while a pull runs', async () => {
    const html = await render(overview({ pull: pull('running') }))
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Pulling<\/button>/)
  })

  it('offers Pull when idle and marks the front month row', async () => {
    const html = await render(overview())
    expect(html).toMatch(/<button(?![^>]*disabled="")[^>]*>Pull<\/button>/)
    expect(html).toMatch(/aria-current="date"[^>]*><td[^>]*>2026-09-10</)
    expect(html).not.toContain('outside the rollover calendar')
  })

  it('shows the outside-calendar notice in Ember', async () => {
    const html = await render(overview({ front: { ES: null } }))
    expect(html).toMatch(/<p class="text-ember">Today is outside the rollover calendar/)
  })

  it('shows a failed refetch above the last data, but leaves a 401 to the shell banner', async () => {
    const html = await render(overview(), new ApiError(500, '{"detail":"boom"}'))
    expect(html).toContain('<p role="alert" class="text-red">500: boom</p>')
    expect(html).toContain('Rollover calendar')
    expect(await render(overview(), new ApiError(401, ''))).not.toContain('class="text-red">')
  })
})

describe('PullView', () => {
  it('shows a 409 inline', () => {
    const html = renderToStaticMarkup(
      <PullView
        pull={pull('idle')}
        error={new ApiError(409, '{"detail":"a pull is already running"}')}
        pending={false}
        onPull={() => {}}
      />,
    )
    expect(html).toContain('<p role="alert" class="text-red">409: a pull is already running</p>')
  })
})
