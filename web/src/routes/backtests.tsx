import { createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const backtestsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/backtests',
  component: () => (
    <Page title="Backtests">
      <p className="max-w-[44ch] text-ink-2">Not built yet. Jobs, results and comparisons land here.</p>
    </Page>
  ),
})
