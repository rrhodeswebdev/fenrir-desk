import { useQuery } from '@tanstack/react-query'
import { createRoute } from '@tanstack/react-router'
import { apiFetch } from '@/api'
import { type Report, ReportView } from '@/backtests/Report'
import { Page } from '@/components/page'
import { pageError } from '@/errorText'
import { rootRoute } from './root'

export const backtestReportRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/backtests/$key',
  component: BacktestReport,
})

function BacktestReport() {
  const { key } = backtestReportRoute.useParams()
  const report = useQuery({
    queryKey: ['backtests', 'results', key],
    queryFn: () => apiFetch<Report>(`/api/backtests/results/${encodeURIComponent(key)}`),
  })
  // A failed refetch keeps the last report on screen, so the error shows above it too.
  const message = pageError(report.error)
  return (
    <Page title="Report" strip={<span className="value min-w-0 break-all">{key}</span>}>
      <div className="grid gap-8">
        {message && (
          <p role="alert" className="text-red">
            {message}
          </p>
        )}
        {report.isPending && <p className="text-ink-3">Loading…</p>}
        {report.data && <ReportView report={report.data} />}
      </div>
    </Page>
  )
}
