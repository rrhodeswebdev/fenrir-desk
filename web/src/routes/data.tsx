import { createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const dataRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/data',
  component: () => (
    <Page title="Data">
      <p className="max-w-[44ch] text-ink-2">Not built yet. Bar coverage, rolls and history pulls land here.</p>
    </Page>
  ),
})
