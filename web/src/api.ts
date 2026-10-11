// The session is an HttpOnly cookie: the page never sees it. Same-origin requests carry it.

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
  ) {
    super(`${status}: ${body}`)
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  // fetch sends a string body as text/plain; FastAPI only parses JSON bodies.
  if (typeof init.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const res = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  const text = await res.text()
  if (!res.ok) throw new ApiError(res.status, text)
  // 204, or a 202 with no body: nothing to parse.
  return (text ? JSON.parse(text) : undefined) as T
}

export const wsUrl = (path: string): string =>
  `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`
