import { createRouter } from '@tanstack/react-router'
import { backtestReportRoute } from './routes/backtest-report'
import { backtestsRoute } from './routes/backtests'
import { dataRoute } from './routes/data'
import { liveRoute } from './routes/live'
import { liveJournalRoute } from './routes/live-journal'
import { rootRoute } from './routes/root'
import { rulesRoute } from './routes/rules'
import { settingsRoute } from './routes/settings'
import { strategiesRoute } from './routes/strategies'
import { workbenchRoute } from './routes/workbench'

export const routeTree = rootRoute.addChildren([
  workbenchRoute,
  rulesRoute,
  dataRoute,
  backtestsRoute,
  backtestReportRoute,
  strategiesRoute,
  liveRoute,
  liveJournalRoute,
  settingsRoute,
])

export const router = createRouter({ routeTree })

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
