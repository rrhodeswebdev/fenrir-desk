import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { routeTree } from '@/router'
import { type Choices, type Settings, DLL_HELP } from './settings'

const settings: Settings = {
  lse_api_key: '••••0001',
  nt_url: 'http://127.0.0.1:7890',
  nt_token: '••••',
  holdout_months: 12,
  safety_margin_pct: 80,
  stale_seconds: 90,
  nested_git: [],
}
const choices: Choices = {
  'lucid-direct-50k': { eval_price: 0, dll_on: true },
  'lucid-flex-50k': { eval_price: 97.5, dll_on: false },
}

async function render(s: Settings = settings) {
  const client = new QueryClient()
  client.setQueryData(['settings'], s)
  client.setQueryData(['settings', 'accounts'], choices)
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ['/settings'] }) })
  await router.load()
  return renderToString(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

/** The <input> whose label (through its for= id) starts with `label`. */
function input(html: string, label: string): string {
  const id = html.match(new RegExp(`<label for="([^"]+)"[^>]*><span class="label">${label}</span>`))?.[1]
  return html.match(new RegExp(`<input[^>]*id="${id}"[^>]*>`))?.[0] ?? `no input labelled ${label}`
}

describe('settings page', () => {
  it('shows every setting, secrets as their masks, and Save disabled until an edit', async () => {
    const html = await render()
    expect([...html.matchAll(/<legend[^>]*>([^<]+)</g)].map((m) => m[1])).toEqual(['Connections', 'Backtest and live'])
    expect(input(html, 'NinjaTrader address')).toContain('value="http://127.0.0.1:7890"')
    expect(input(html, 'NinjaTrader token')).toMatch(/type="text"[^>]*value="••••"/)
    expect(input(html, 'LSE API key')).toMatch(/type="text"[^>]*value="••••0001"/)
    expect(input(html, 'LSE API key')).toMatch(/autoComplete="new-password"/i)
    expect(input(html, 'Hold-out')).toMatch(/type="number"[^>]*value="12"/)
    expect(input(html, 'Safety margin')).toContain('value="80"')
    expect(input(html, 'Stale threshold')).toContain('value="90"')
    expect(html).toContain('<span class="text-ink-3">% of the daily loss limit or of the drawdown room at session start</span>')
    expect(html.match(/<button[^>]*type="submit"[^>]*disabled=""[^>]*>Save<\/button>/g)).toHaveLength(2)
    expect(html).not.toContain('role="alert"')
  })

  it('lists each account with its price and daily loss limit switch, described by the dll_on help', async () => {
    const html = await render()
    expect(html).toMatch(/aria-label="lucid-flex-50k evaluation price"[^>]*value="97.5"/)
    expect(html).toMatch(/aria-checked="true"[^>]*aria-label="lucid-direct-50k daily loss limit"/)
    expect(html).toMatch(/aria-checked="false"[^>]*aria-label="lucid-flex-50k daily loss limit"/)
    const helpId = html.match(/<p id="([^"]+)"[^>]*>Daily loss limit: /)?.[1]
    expect(html).toMatch(new RegExp(`aria-label="lucid-flex-50k daily loss limit"[^>]*aria-describedby="${helpId}"`))
    expect(DLL_HELP).toContain('re-run the backtest and re-arm the account')
  })

  it('warns about a .git the terminal left in the repo', async () => {
    const html = await render({ ...settings, nested_git: ['strategies/.git'] })
    expect(html).toMatch(/<p role="alert"[^>]*>The terminal left a .git in the repo: <!-- -->strategies\/.git/)
    expect(html).toContain('find . -name .git -not -path ./.git')
  })
})
