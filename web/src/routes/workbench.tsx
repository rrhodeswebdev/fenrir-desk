import { createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: () => (
    <Page title="Workbench">
      <p className="max-w-[44ch] text-ink-2">Not built yet. Terminal and latest report land here.</p>
    </Page>
  ),
})
