import { ApiError } from './api'

export const SESSION_EXPIRED =
  'Session expired: restart the server (`docker compose restart`) and open the new launch link'

/** FastAPI errors are `{"detail": "..."}` or, for 422, `{"detail": [{msg, ...}]}`; anything else is shown as sent. */
function detail(body: string): string {
  try {
    const parsed: unknown = JSON.parse(body)
    if (typeof parsed === 'object' && parsed !== null && 'detail' in parsed) {
      const d = parsed.detail
      if (typeof d === 'string') return d
      if (Array.isArray(d) && d.length > 0 && d.every((x) => typeof x?.msg === 'string'))
        return d.map((x: { msg: string }) => x.msg).join('; ')
    }
  } catch {
    // not JSON: fall through to the raw body
  }
  return body
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.status === 401 ? SESSION_EXPIRED : `${e.status}: ${detail(e.body) || 'no details'}`
  return e instanceof Error ? e.message : String(e)
}
