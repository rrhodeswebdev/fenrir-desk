import { createRoute } from '@tanstack/react-router'
import { RunForm } from '@/backtests/RunForm'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const backtestsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/backtests',
  component: () => (
    <Page title="Backtests">
      <RunForm />
    </Page>
  ),
})
