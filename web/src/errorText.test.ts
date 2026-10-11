import { describe, expect, it } from 'vitest'
import { ApiError } from './api'
import { SESSION_EXPIRED, errorText, pageError } from './errorText'

describe('errorText', () => {
  it.each([
    [new ApiError(401, '{"detail":"no session"}'), SESSION_EXPIRED],
    [new ApiError(404, '{"detail":"Not found"}'), '404: Not found'],
    [new ApiError(500, 'Internal Server Error'), '500: Internal Server Error'],
    [new ApiError(422, '{"detail":[{"loc":["body","a"],"msg":"Field required"}]}'), '422: Field required'],
    [
      new ApiError(422, '{"detail":[{"loc":["body","a"],"msg":"Field required"},{"loc":["body","b"],"msg":"Input should be a valid integer"}]}'),
      '422: Field required; Input should be a valid integer',
    ],
    [new ApiError(409, '{"error":"already_armed"}'), '409: {"error":"already_armed"}'],
    [new ApiError(502, ''), '502: no details'],
    [new TypeError('Failed to fetch'), 'Failed to fetch'],
    ['boom', 'boom'],
  ])('%o → %s', (e, expected) => {
    expect(errorText(e)).toBe(expected)
  })

  it('pageError leaves a 401 to the shell banner and reports anything else', () => {
    expect(pageError(new ApiError(401, ''))).toBeNull()
    expect(pageError(new ApiError(500, 'boom'))).toBe('500: boom')
  })

  it('names the restart command for an expired session', () => {
    expect(SESSION_EXPIRED).toBe(
      'Session expired: restart the server (`docker compose restart`) and open the new launch link',
    )
  })
})
