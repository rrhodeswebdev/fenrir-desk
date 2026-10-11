import { describe, expect, it } from 'vitest'
import { ApiError } from './api'
import { errorsAt, fieldErrors, getIn, setIn } from './rulesForm'

const rules = {
  funded: { max_drawdown: { amount: 2000, locks_at: 2100 }, payout: { max_amount: null } },
  session: { tz: 'America/New_York' },
  scaling: [{ max_minis: 2 }, { max_minis: 4 }],
}

describe('setIn', () => {
  it('replaces one value without mutating and shares untouched branches', () => {
    const before = structuredClone(rules)
    const next = setIn(rules, ['funded', 'max_drawdown', 'amount'], 2500)
    expect(next.funded.max_drawdown).toEqual({ amount: 2500, locks_at: 2100 })
    expect(rules).toEqual(before)
    expect(next.session).toBe(rules.session)
    expect(next.funded.payout).toBe(rules.funded.payout)
    expect(next.scaling).toBe(rules.scaling)
  })

  it('sets null, creates missing objects and replaces at the root', () => {
    expect(setIn(rules, ['funded', 'max_drawdown', 'locks_at'], null).funded.max_drawdown.locks_at).toBeNull()
    expect(setIn({ evaluation: null }, ['evaluation', 'consistency', 'max_day_pct'], 50)).toEqual({ evaluation: { consistency: { max_day_pct: 50 } } })
    expect(setIn(rules, [], 1)).toBe(1)
  })

  it('keeps arrays as arrays', () => {
    const next = setIn(rules, ['scaling', 1, 'max_minis'], 5)
    expect(next.scaling).toEqual([{ max_minis: 2 }, { max_minis: 5 }])
    expect(next.scaling[0]).toBe(rules.scaling[0])
  })
})

describe('getIn', () => {
  it('reads a path and gives undefined past a missing or null node', () => {
    expect(getIn(rules, ['scaling', 1, 'max_minis'])).toBe(4)
    expect(getIn(rules, ['funded', 'payout', 'max_amount', 'x'])).toBeUndefined()
    expect(getIn(rules, ['nope', 'x'])).toBeUndefined()
  })
})

describe('errorsAt and fieldErrors', () => {
  const body = { detail: 'The rules do not validate.', errors: [{ loc: ['funded', 'max_drawdown', 'amount'], msg: 'Input should be greater than 0' }, { loc: ['funded', 'max_drawdown'], msg: 'locks_at is below amount' }] }

  it('reads the errors of a 422 and matches them by exact loc', () => {
    const errors = fieldErrors(new ApiError(422, JSON.stringify(body)))
    expect(errors).toEqual(body.errors)
    expect(errorsAt(errors, ['funded', 'max_drawdown', 'amount'])).toEqual(['Input should be greater than 0'])
    expect(errorsAt(errors, ['funded', 'max_drawdown'])).toEqual(['locks_at is below amount'])
    expect(errorsAt(errors, ['evaluation', 'max_drawdown', 'amount'])).toEqual([])
  })

  it('gives [] for other statuses, other errors and bodies that are not the contract', () => {
    expect(fieldErrors(new ApiError(404, JSON.stringify(body)))).toEqual([])
    expect(fieldErrors(new Error('network'))).toEqual([])
    expect(fieldErrors(new ApiError(422, 'not json'))).toEqual([])
    expect(fieldErrors(new ApiError(422, '{"detail": [{"msg": "x"}]}'))).toEqual([])
  })
})
