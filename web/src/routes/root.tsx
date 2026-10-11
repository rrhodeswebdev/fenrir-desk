import { type QueryClient, useQueryClient } from '@tanstack/react-query'
import { Link, Outlet, createRootRoute, useLocation } from '@tanstack/react-router'
import { useEffect, useRef, useSyncExternalStore } from 'react'
import mark from '../../../assets/brand/logo/fenrir-desk-mark-on-dark.svg'
import { ApiError } from '@/api'
import { Page } from '@/components/page'
import { SESSION_EXPIRED } from '@/errorText'

const pages = [
  ['/', 'Workbench'],
  ['/rules', 'Rules'],
  ['/data', 'Data'],
  ['/backtests', 'Backtests'],
  ['/strategies', 'Strategies'],
  ['/live', 'Live'],
  ['/settings', 'Settings'],
] as const

/** True while any query's last error is a 401: the session cookie is gone or stale. */
export const sessionExpired = (client: QueryClient): boolean =>
  client
    .getQueryCache()
    .getAll()
    .some((q) => q.state.error instanceof ApiError && q.state.error.status === 401)

function Shell() {
  const client = useQueryClient()
  const snapshot = () => sessionExpired(client)
  const expired = useSyncExternalStore((onChange) => client.getQueryCache().subscribe(onChange), snapshot, snapshot)
  const nav = useRef<HTMLElement>(null)
  const { pathname } = useLocation()
  // At <= 760px the rail is a scrolling strip: keep the active page in view.
  useEffect(() => {
    nav.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [pathname])

  return (
    <div className="grid min-h-screen grid-cols-[184px_1fr] max-md:grid-cols-1 max-md:grid-rows-[auto_1fr]">
      <aside className="sticky top-0 flex h-screen flex-col gap-8 py-6 max-md:static max-md:h-auto max-md:flex-row max-md:items-center max-md:gap-3 max-md:overflow-x-auto max-md:border-b max-md:px-4 max-md:py-3">
        <img src={mark} alt="Fenrir Desk" className="mx-6 size-11 max-md:m-0 max-md:size-8 max-md:flex-none" />
        <nav ref={nav} aria-label="Pages">
          <ul className="max-md:flex">
            {pages.map(([to, name]) => (
              <li key={to}>
                <Link
                  to={to}
                  activeOptions={{ exact: to === '/' }}
                  className="step flex h-11 items-center justify-between px-6 font-mono text-[11px] leading-none font-medium tracking-label whitespace-nowrap text-ink-3 uppercase hover:text-ink max-md:h-10 max-md:gap-2 max-md:px-3 aria-[current=page]:mx-3 aria-[current=page]:bg-ground-deep aria-[current=page]:px-3 aria-[current=page]:text-ink aria-[current=page]:shadow-[inset_0_0_0_1px_var(--ink-3)] aria-[current=page]:after:h-4 aria-[current=page]:after:w-2 aria-[current=page]:after:bg-ember"
                >
                  {name}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </aside>
      <main className="min-w-0 border-l max-md:border-l-0">
        {expired && (
          <p role="alert" className="border-b px-8 py-3 text-red max-md:px-4">
            {SESSION_EXPIRED}
          </p>
        )}
        <Outlet />
      </main>
    </div>
  )
}

export const rootRoute = createRootRoute({
  component: Shell,
  notFoundComponent: () => (
    <Page title="Not found">
      <p className="text-ink-2">No page at this address.</p>
    </Page>
  ),
})
