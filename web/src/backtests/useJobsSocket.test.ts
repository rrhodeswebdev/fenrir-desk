import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { type Job, type JobItem, finished, jobsKey, receiveJob, upsertJob } from './useJobsSocket'

const item = (status: JobItem['status']): JobItem => ({
  strategy: 'strategies/orb.py',
  account: 'lucid-flex-50k',
  contract: 'MNQ',
  status,
  key: null,
  error: null,
  progress: [0, 0],
})
const job = (id: string, created_at: string, ...statuses: JobItem['status'][]): Job => ({
  id,
  created_at,
  include_holdout: true,
  items: statuses.map(item),
})

const older = job('a', '2026-10-11T10:00:00.000000Z', 'done')
const newer = job('b', '2026-10-11T11:00:00.000000Z', 'pending')

describe('upsertJob', () => {
  it('adds a new job newest first', () => {
    expect(upsertJob([older], newer).map((j) => j.id)).toEqual(['b', 'a'])
    expect(upsertJob([newer], older).map((j) => j.id)).toEqual(['b', 'a'])
  })

  it('replaces the job with the same id in place', () => {
    const running = job('b', newer.created_at, 'running')
    const jobs = [newer, older]
    expect(upsertJob(jobs, running)).toEqual([running, older])
    expect(jobs).toEqual([newer, older]) // never mutated
  })
})

describe('finished', () => {
  it('counts done and error items, and none for an unknown job', () => {
    expect(finished(job('c', '', 'pending', 'running', 'done', 'error'))).toBe(2)
    expect(finished(undefined)).toBe(0)
  })
})

describe('receiveJob', () => {
  const setup = () => {
    const qc = new QueryClient()
    qc.setQueryData<Job[]>(jobsKey, [newer, older])
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    return { qc, invalidate }
  }

  it('upserts the job without refreshing results while nothing finished', () => {
    const { qc, invalidate } = setup()
    receiveJob(qc, job('b', newer.created_at, 'running'))
    expect(qc.getQueryData<Job[]>(jobsKey)?.[0]?.items[0]?.status).toBe('running')
    receiveJob(qc, older) // the on-connect snapshot repeats a finished job: nothing new finished
    expect(invalidate).not.toHaveBeenCalled()
  })

  it('refreshes results and compare when an item finishes, with a report or an error', () => {
    for (const status of ['done', 'error'] as const) {
      const { qc, invalidate } = setup()
      receiveJob(qc, job('b', newer.created_at, status))
      expect(invalidate.mock.calls.map(([f]) => f?.queryKey)).toEqual([
        ['backtests', 'results'],
        ['backtests', 'compare'],
      ])
    }
  })
})
