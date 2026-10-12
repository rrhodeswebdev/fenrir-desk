import { type QueryClient, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { wsUrl } from '@/api'

// GET /api/backtests/jobs, WS /ws/jobs (docs/contracts.md "Plan 14 — backtest jobs API")
export type JobItem = {
  strategy: string
  account: string
  contract: string
  status: 'pending' | 'running' | 'done' | 'error'
  key: string | null
  error: string | null
  progress: [number, number]
}
export type Job = { id: string; created_at: string; include_holdout: boolean; items: JobItem[] }

export const jobsKey = ['backtests', 'jobs']

/** Replaces the job with the same id (or adds it), newest first. */
export const upsertJob = (jobs: Job[], job: Job): Job[] =>
  [job, ...jobs.filter((j) => j.id !== job.id)].sort((a, b) => b.created_at.localeCompare(a.created_at))

/** Items that finished, with a report (`done`) or without (`error`). */
export const finished = (job?: Job): number =>
  job?.items.filter((i) => i.status === 'done' || i.status === 'error').length ?? 0

/** One pushed job into the cache; results and compare refetch when one of its items finished. */
export function receiveJob(qc: QueryClient, job: Job): void {
  const before = qc.getQueryData<Job[]>(jobsKey)?.find((j) => j.id === job.id)
  qc.setQueryData<Job[]>(jobsKey, (jobs = []) => upsertJob(jobs, job))
  if (finished(job) > finished(before)) {
    void qc.invalidateQueries({ queryKey: ['backtests', 'results'] })
    void qc.invalidateQueries({ queryKey: ['backtests', 'compare'] })
  }
}

/**
 * Keeps `['backtests', 'jobs']` current from `/ws/jobs` and refreshes results and compare
 * when an item finishes. Open it only once the GET has landed: the socket's first frames are
 * a newer snapshot, so an older GET can never overwrite them. Returns true once the socket closed.
 */
export function useJobsSocket(enabled: boolean): boolean {
  const qc = useQueryClient()
  const [closed, setClosed] = useState(false)
  useEffect(() => {
    if (!enabled) return
    const ws = new WebSocket(wsUrl('/ws/jobs'))
    ws.onmessage = (e: MessageEvent<string>) => receiveJob(qc, JSON.parse(e.data) as Job)
    // shortcut: no reconnect (the contract asks for none); the list says so and a reload resumes.
    ws.onclose = () => setClosed(true)
    return () => {
      ws.onclose = null
      ws.close()
    }
  }, [qc, enabled])
  return closed
}
