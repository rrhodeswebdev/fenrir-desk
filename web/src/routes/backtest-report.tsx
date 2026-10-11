import { createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const backtestReportRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/backtests/$key',
  component: BacktestReport,
})

function BacktestReport() {
  const { key } = backtestReportRoute.useParams()
  return (
    <Page title="Report" strip={<span className="value">{key}</span>}>
      <p className="max-w-[44ch] text-ink-2">Not built yet. Pass and payout rates for this run land here.</p>
    </Page>
  )
}
