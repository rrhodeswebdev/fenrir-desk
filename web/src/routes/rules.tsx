import { useQuery } from '@tanstack/react-query'
import { Link, createRoute } from '@tanstack/react-router'
import { apiFetch } from '@/api'
import { Page } from '@/components/page'
import { StateText, type Tone } from '@/components/state-text'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { pageError } from '@/errorText'
import { rootRoute } from './root'

/** One `GET /api/rules` row (contracts: Plan 15). */
export type RuleRow = {
  id: string
  verified_on: string | null
  age_days: number | null
  stale: boolean | null
  error: string | null
}

/** The row's status: Red for a file that does not load, Ember when never verified, future-dated or older than 30 days. */
export function freshness(row: RuleRow): { tone: Tone; text: string } {
  if (row.error) return { tone: 'alarm', text: 'Invalid file' }
  if (row.verified_on === null || row.age_days === null) return { tone: 'caution', text: 'Never verified' }
  const age = row.age_days
  if (age < 0) return { tone: 'caution', text: 'Verified in the future' }
  const text = age === 0 ? 'Verified today' : `Verified ${age} ${age === 1 ? 'day' : 'days'} ago`
  return { tone: row.stale ? 'caution' : 'normal', text }
}

function RulesPage() {
  const { account } = rulesRoute.useSearch()
  const rows = useQuery({ queryKey: ['rules'], queryFn: () => apiFetch<RuleRow[]>('/api/rules') })
  const error = pageError(rows.error)
  return (
    <Page title="Rules">
      {error && <p className="text-red">{error}</p>}
      {rows.data?.length === 0 && <p className="text-ink-2">No rule files.</p>}
      {!!rows.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Account</TableHead>
              <TableHead scope="col" className="max-md:hidden">
                Verified on
              </TableHead>
              <TableHead scope="col">Status</TableHead>
              <TableHead scope="col">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.data.map((r) => {
              const { tone, text } = freshness(r)
              return (
                <TableRow key={r.id} data-state={r.id === account ? 'selected' : undefined}>
                  <TableCell className="value">{r.id}</TableCell>
                  <TableCell className={r.verified_on ? 'value max-md:hidden' : 'text-ink-3 max-md:hidden'}>{r.verified_on ?? '—'}</TableCell>
                  <TableCell className="whitespace-normal">
                    <StateText tone={tone}>{text}</StateText>
                    {r.error && <p className="max-w-[72ch] text-red wrap-anywhere">{r.error}</p>}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button asChild size="sm">
                      <Link to="/rules" search={{ account: r.id }} aria-label={`Edit ${r.id}`}>
                        Edit
                      </Link>
                    </Button>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
    </Page>
  )
}

export const rulesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/rules',
  // ?account=<id> selects the file the editor opens (#53).
  validateSearch: (search: Record<string, unknown>): { account?: string } =>
    typeof search.account === 'string' ? { account: search.account } : {},
  component: RulesPage,
})
