import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { apiFetch } from '@/api'
import { StateText, type Tone } from '@/components/state-text'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { pageError } from '@/errorText'
import { type Job, type JobItem, jobsKey, useJobsSocket } from './useJobsSocket'

const RECENT = 5

// Colour only for abnormal state: a failed item is alarm, the rest are normal or idle.
const statusTone: Record<JobItem['status'], Tone> = { pending: 'idle', running: 'normal', done: 'normal', error: 'alarm' }

/** Whole percent of a running item's attempts, else nothing. */
export const progressText = ({ status, progress: [done, total] }: JobItem): string =>
  status === 'running' && total > 0 ? `${Math.floor((done * 100) / total)}%` : ''

export function JobList() {
  // No polling and no refetch: once this GET lands, /ws/jobs keeps the query current.
  const jobs = useQuery({ queryKey: jobsKey, queryFn: () => apiFetch<Job[]>('/api/backtests/jobs'), staleTime: Infinity })
  const closed = useJobsSocket(jobs.isSuccess)
  return <JobListView jobs={jobs.data} pending={jobs.isPending} error={pageError(jobs.error)} closed={closed} />
}

export function JobListView({ jobs, pending, error, closed }: { jobs?: Job[]; pending: boolean; error: string | null; closed: boolean }) {
  const recent = (jobs ?? []).slice(0, RECENT)
  return (
    <section aria-labelledby="jobs" className="grid gap-3 border-t pt-8">
      <h2 id="jobs">{`Jobs · latest ${RECENT}`}</h2>
      {error && (
        <p role="alert" className="text-red">
          {error}
        </p>
      )}
      {closed && (
        <p role="status" className="text-ember">
          Live updates stopped. Reload the page to resume them.
        </p>
      )}
      {pending && <p className="text-ink-3">Loading…</p>}
      {jobs && recent.length === 0 && <p className="text-ink-3">No jobs yet.</p>}
      {recent.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Strategy</TableHead>
              <TableHead scope="col">Account</TableHead>
              <TableHead scope="col">Contract</TableHead>
              <TableHead scope="col">Status</TableHead>
              <TableHead scope="col">Detail</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {recent.flatMap((job) =>
              job.items.map((it, i) => (
                <TableRow key={`${job.id}-${i}`}>
                  <TableCell className="value">{it.strategy}</TableCell>
                  <TableCell>{it.account}</TableCell>
                  <TableCell>{it.contract}</TableCell>
                  <TableCell className="whitespace-normal">
                    <StateText tone={statusTone[it.status]}>{it.status}</StateText>
                    {it.error && <p className="max-w-[72ch] text-red wrap-anywhere">{it.error}</p>}
                  </TableCell>
                  <TableCell>
                    {it.key ? (
                      <Link to="/backtests/$key" params={{ key: it.key }}>
                        Report
                      </Link>
                    ) : (
                      progressText(it)
                    )}
                  </TableCell>
                </TableRow>
              )),
            )}
          </TableBody>
        </Table>
      )}
    </section>
  )
}
