import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory, createRouter } from '@tanstack/react-router'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api'
import { routeTree } from '@/router'
import { type Choices, type Settings, DLL_HELP, RESTART_LATE, RESTART_POLL_MS, Restart, restartServer } from './settings'

const settings: Settings = {
  lse_api_key: '••••0001',
  nt_url: 'http://127.0.0.1:7890',
  nt_token: '••••',
  holdout_months: 12,
  safety_margin_pct: 80,
  stale_seconds: 90,
  nested_git: [],
  can_restart: false,
  restart_pending: false,
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
    expect(html).toContain('find data rules strategies -iname .git')
  })
})

describe('apply and restart', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('shows the button in Docker only once the secrets file changed', async () => {
    const docker = { ...settings, can_restart: true }
    expect(await render(docker)).toContain('applies after Apply and restart.')
    expect(await render(docker)).not.toContain('>Apply and restart</button>')
    expect(await render({ ...docker, restart_pending: true })).toMatch(/<button[^>]*>Apply and restart<\/button>/)
  })

  it('keeps an empty status line there from the start, so its changes are announced', async () => {
    const html = await render({ ...settings, can_restart: true })
    expect(html).toMatch(/<p role="status" tabindex="-1" class="[^"]*"><\/p>/i)
  })

  it('is disabled while there are unsaved edits or a save in flight', () => {
    const row = (unsaved: boolean) =>
      renderToString(
        <QueryClientProvider client={new QueryClient()}>
          <Restart server={{ ...settings, can_restart: true, restart_pending: true }} unsaved={unsaved} />
        </QueryClientProvider>,
      )
    expect(row(true)).toMatch(/<button[^>]*disabled=""[^>]*>Apply and restart<\/button>/)
    expect(row(false)).not.toMatch(/disabled=""[^>]*>Apply and restart/)
  })

  it('natively says to restart the server, with no button', async () => {
    expect(await render()).not.toContain('Restart the server to apply')
    const html = await render({ ...settings, restart_pending: true })
    expect(html).toContain('Restart the server to apply the saved settings.')
    expect(html).not.toContain('Apply and restart</button>')
  })

  /** fetch answering each `/api/health` from `health` in turn (true: 200, false: refused), the POST with `post`. */
  function stubFetch(post: Response, health: boolean[]) {
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string, init?: RequestInit) => {
        calls.push(`${init?.method ?? 'GET'} ${path}`)
        if (path === '/api/settings/restart') return post
        if (health.shift() ?? true) return new Response('{"ok":true}')
        throw new TypeError('Failed to fetch')
      }),
    )
    return calls
  }

  it('posts, then waits for the old server to stop and the new one to answer', async () => {
    const calls = stubFetch(new Response(null, { status: 202 }), [true, false, false, true])
    const waits: number[] = []
    await restartServer(async (ms) => void waits.push(ms))
    expect(calls).toEqual(['POST /api/settings/restart', ...Array(4).fill('GET /api/health')])
    expect(waits).toEqual([RESTART_POLL_MS, RESTART_POLL_MS])
  })

  it('a refusal is the 409 with its reason, and nothing is polled', async () => {
    const calls = stubFetch(new Response('{"detail":"Disarm every account first (armed: Sim101)."}', { status: 409 }), [])
    const e = await restartServer(async () => {}).catch((x: unknown) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect((e as ApiError).status).toBe(409)
    expect(calls).toEqual(['POST /api/settings/restart'])
  })

  it('gives up with RESTART_LATE when the server never comes back', async () => {
    stubFetch(new Response(null, { status: 202 }), Array(1000).fill(false))
    let t = 0
    const e = await restartServer(async (ms) => void (t += ms), () => t).catch((x: unknown) => x)
    expect(e).toEqual(new Error(RESTART_LATE))
    expect(RESTART_LATE).not.toContain('`') // plain text in the error line
    expect(t).toBeGreaterThanOrEqual(60_000)
  })
})
