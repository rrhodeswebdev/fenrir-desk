import { createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const liveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/live',
  component: () => (
    <Page title="Live">
      <p className="max-w-[44ch] text-ink-2">Not built yet. Armed accounts, limits and the kill switch land here.</p>
    </Page>
  ),
})
