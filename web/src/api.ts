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
  if (init.body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const res = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  if (!res.ok) throw new ApiError(res.status, await res.text())
  return (await res.json()) as T
}

export const wsUrl = (path: string): string =>
  `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`
