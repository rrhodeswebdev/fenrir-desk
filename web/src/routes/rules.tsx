import { createRoute } from '@tanstack/react-router'
import { Page } from '@/components/page'
import { rootRoute } from './root'

export const rulesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/rules',
  component: () => (
    <Page title="Rules">
      <p className="max-w-[44ch] text-ink-2">Not built yet. Account rule files and their verified dates land here.</p>
    </Page>
  ),
})
