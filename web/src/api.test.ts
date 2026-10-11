import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiFetch, wsUrl } from './api'

afterEach(() => vi.unstubAllGlobals())

function mockFetch(res: Response) {
  const fetch = vi.fn(async (_path: string, _init: RequestInit) => res)
  vi.stubGlobal('fetch', fetch)
  return fetch
}

describe('apiFetch', () => {
  it('sends a same-origin request with no token and parses JSON', async () => {
    const fetch = mockFetch(new Response('{"ok":true}', { status: 200 }))
    expect(await apiFetch<{ ok: boolean }>('/api/x')).toEqual({ ok: true })
    const [path, init] = fetch.mock.calls[0]!
    expect(path).toBe('/api/x')
    expect(init.credentials).toBe('same-origin')
    expect([...new Headers(init.headers).keys()]).toEqual([])
  })

  it('marks a body as JSON and keeps the caller init', async () => {
    const fetch = mockFetch(new Response('{}'))
    await apiFetch('/api/x', { method: 'POST', body: '{"a":1}' })
    const [, init] = fetch.mock.calls[0]!
    expect(init.method).toBe('POST')
    expect(init.body).toBe('{"a":1}')
    expect(new Headers(init.headers).get('Content-Type')).toBe('application/json')
  })

  it.each([204, 202])('returns undefined for an empty %i body', async (status) => {
    mockFetch(new Response(null, { status }))
    expect(await apiFetch('/api/x', { method: 'POST' })).toBeUndefined()
  })

  it('leaves Content-Type to fetch for a non-string body', async () => {
    const fetch = mockFetch(new Response('{}'))
    await apiFetch('/api/x', { method: 'POST', body: new FormData() })
    const [, init] = fetch.mock.calls[0]!
    expect(new Headers(init.headers).has('Content-Type')).toBe(false)
  })

  it('throws ApiError with status and body text on non-2xx', async () => {
    mockFetch(new Response('Internal Server Error', { status: 500 }))
    const err = await apiFetch('/api/x').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 500, body: 'Internal Server Error' })
  })
})

describe('wsUrl', () => {
  it.each([
    ['http://localhost:5173/live', 'ws://localhost:5173/ws/jobs'],
    ['https://localhost:8000/', 'wss://localhost:8000/ws/jobs'],
  ])('builds from %s', (page, expected) => {
    vi.stubGlobal('location', new URL(page))
    expect(wsUrl('/ws/jobs')).toBe(expected)
  })
})
