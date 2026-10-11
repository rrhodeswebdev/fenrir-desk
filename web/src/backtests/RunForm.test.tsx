import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToStaticMarkup, renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api'
import { routeTree } from '@/router'
import { type Options, type Picks, RunFormView, canRun, jobBody, noPicks, postJob, toggle } from './RunForm'

afterEach(() => vi.unstubAllGlobals())

const options: Options = {
  strategies: ['strategies/orb.py', 'strategies/vwap.py'],
  accounts: ['lucid-flex-50k', 'lucid-pro-25k'],
  contracts: ['ES', 'MES', 'NQ', 'MNQ'],
}
const picks: Picks = { strategies: ['strategies/orb.py'], accounts: ['lucid-flex-50k'], contracts: [] }

const view = (over: Partial<Parameters<typeof RunFormView>[0]> = {}) =>
  renderToStaticMarkup(
    <RunFormView options={options} picks={picks} onToggle={() => {}} onRun={() => {}} pending={false} error={null} {...over} />,
  )

/** The form's submit button, start tag to end tag. */
const submit = (html: string): string => html.match(/<button[^>]*type="submit"[^>]*>[^<]*<\/button>/)?.[0] ?? ''

describe('toggle', () => {
  it('ticks a value on one axis in tick order, and unticks it again', () => {
    const one = toggle(noPicks, 'contracts', 'MNQ')
    const two = toggle(one, 'contracts', 'ES')
    expect(two).toEqual({ ...noPicks, contracts: ['MNQ', 'ES'] })
    expect(toggle(two, 'contracts', 'MNQ')).toEqual({ ...noPicks, contracts: ['ES'] })
    expect(noPicks.contracts).toEqual([])  // never mutated
  })
})

describe('jobBody', () => {
  it('sends null contracts when none is ticked, else the ticked ones', () => {
    expect(jobBody(picks)).toEqual({ strategies: ['strategies/orb.py'], accounts: ['lucid-flex-50k'], contracts: null })
    expect(jobBody({ ...picks, contracts: ['MNQ'] }).contracts).toEqual(['MNQ'])
  })
})

describe('canRun', () => {
  it.each([
    [noPicks, false],
    [{ ...picks, accounts: [] }, false],
    [{ ...picks, strategies: [] }, false],
    [picks, true],
  ])('%#', (p, expected) => expect(canRun(p)).toBe(expected))
})

describe('postJob', () => {
  it('posts the selected grid to /api/backtests/jobs and returns the job id', async () => {
    const fetch = vi.fn(async (_path: string, _init: RequestInit) => new Response('{"job_id":"j1"}'))
    vi.stubGlobal('fetch', fetch)
    expect(await postJob({ ...picks, contracts: ['MNQ', 'NQ'] })).toEqual({ job_id: 'j1' })
    const [path, init] = fetch.mock.calls[0]!
    expect(path).toBe('/api/backtests/jobs')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      strategies: ['strategies/orb.py'],
      accounts: ['lucid-flex-50k'],
      contracts: ['MNQ', 'NQ'],
    })
  })
})

describe('RunFormView', () => {
  it('lists every option as a labelled checkbox, ticking the picks', () => {
    const html = view()
    for (const v of [...options.strategies, ...options.accounts, ...options.contracts]) expect(html).toContain(`>${v}</label>`)
    expect([...html.matchAll(/aria-checked="true"/g)]).toHaveLength(2)
    expect(submit(html)).toMatch(/^(?!.*disabled="").*>Run<\/button>$/)
  })

  it('disables Run until a strategy and an account are ticked, and while queuing', () => {
    expect(submit(view({ picks: noPicks }))).toMatch(/disabled="".*>Run<\/button>$/)
    expect(submit(view({ pending: true }))).toMatch(/disabled="".*>Queuing<\/button>$/)
  })

  it('shows the new job', () => {
    expect(view({ jobId: 'j1' })).toContain('<span class="label">Queued job</span><span class="value">j1</span>')
  })

  it("shows a 422's message", () => {
    const error = new ApiError(422, '{"detail":"empty grid: ES fits no strategy"}')
    expect(view({ error })).toContain('<p role="alert" class="text-red">422: empty grid: ES fits no strategy</p>')
  })
})

describe('Backtests page', () => {
  it('renders the run form once the options load', async () => {
    const client = new QueryClient()
    client.setQueryData<Options>(['backtests', 'options'], options)
    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/backtests'] }) })
    await router.load()
    const html = renderToString(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(html).toContain('<h1>Backtests</h1>')
    expect(html).toContain('>strategies/orb.py</label>')
    expect(submit(html)).toMatch(/disabled="".*>Run<\/button>$/)
  })
})
