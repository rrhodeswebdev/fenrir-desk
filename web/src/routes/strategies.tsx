import { createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const strategiesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/strategies',
  component: () => (
    <Page title="Strategies">
      <p className="max-w-[44ch] text-ink-2">Not built yet. Strategy files, params and arming land here.</p>
    </Page>
  ),
})
