import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToStaticMarkup, renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { routeTree } from '@/router'
import { JobListView, progressText } from './JobList'
import type { Job, JobItem } from './useJobsSocket'

const item = (over: Partial<JobItem>): JobItem => ({
  strategy: 'strategies/orb.py',
  account: 'lucid-flex-50k',
  contract: 'MNQ',
  status: 'pending',
  key: null,
  error: null,
  progress: [0, 0],
  ...over,
})
const job = (id: string, items: JobItem[]): Job => ({ id, created_at: `2026-10-11T1${id}:00:00.000000Z`, include_holdout: true, items })

const view = (over: Partial<Parameters<typeof JobListView>[0]> = {}) =>
  renderToStaticMarkup(<JobListView pending={false} error={null} closed={false} {...over} />)

describe('progressText', () => {
  it.each([
    [item({ status: 'running', progress: [1, 3] }), '33%'],
    [item({ status: 'running', progress: [0, 0] }), ''],
    [item({ status: 'pending', progress: [0, 10] }), ''],
    [item({ status: 'done', progress: [10, 10] }), ''],
  ])('%#', (it_, expected) => expect(progressText(it_)).toBe(expected))
})

describe('JobListView', () => {
  it("shows a failed item's error text in its row", () => {
    const html = view({ jobs: [job('1', [item({ status: 'error', error: 'SyntaxError: bad strategy' })])] })
    expect(html).toContain('>error</span><p class="max-w-[72ch] text-red wrap-anywhere">SyntaxError: bad strategy</p></td>')
  })

  it('shows a running item with its progress, one row per item of the latest five jobs', () => {
    const jobs = ['6', '5', '4', '3', '2', '1'].map((id) => job(id, [item({ status: 'running', progress: [1, 2] }), item({})]))
    const html = view({ jobs })
    expect([...html.matchAll(/<tr /g)]).toHaveLength(1 + 5 * 2)
    expect(html).toContain('>running</span></td><td data-slot="table-cell" class="p-2 align-middle whitespace-nowrap [&amp;:has([role=checkbox])]:pr-0 [&amp;&gt;[role=checkbox]]:translate-y-[2px]">50%</td>')
  })

  it('says when there are no jobs, while loading, on a failed GET and once live updates stop', () => {
    expect(view({ jobs: [] })).toContain('No jobs yet.')
    expect(view({ pending: true })).toContain('Loading…')
    expect(view({ jobs: [], error: '500: boom' })).toContain('<p role="alert" class="text-red">500: boom</p>')
    expect(view({ jobs: [], closed: true })).toContain('Live updates stopped. Reload the page to resume them.')
    expect(view({ jobs: [] })).not.toContain('Live updates stopped')
  })
})

describe('Backtests page', () => {
  it('lists the jobs under the run form, linking a done item to its report', async () => {
    const client = new QueryClient()
    client.setQueryData<Job[]>(['backtests', 'jobs'], [job('1', [item({ status: 'done', key: 'k1' })])])
    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/backtests'] }) })
    await router.load()
    const html = renderToString(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    expect(html).toContain('<h2 id="jobs">Jobs · latest 5</h2>')
    expect(html).toMatch(/>done<\/span><\/td><td[^>]*><a[^>]*href="\/backtests\/k1"[^>]*>Report<\/a>/)
  })
})
