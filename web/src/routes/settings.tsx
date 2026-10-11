import { createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/settings',
  component: () => (
    <Page title="Settings">
      <p className="max-w-[44ch] text-ink-2">Not built yet. Secrets, account prices and daily loss settings land here.</p>
    </Page>
  ),
})
