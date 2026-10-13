import { afterEach, describe, expect, it, vi } from 'vitest'
import { resolveInit } from './actions'
import { RESOLVE_TIMEOUT_MS } from './model'

afterEach(() => vi.restoreAllMocks())

describe('resolve request', () => {
  it('posts the account with a client timeout, so a hung hub never leaves Resolve running for ever', () => {
    const timeout = vi.spyOn(AbortSignal, 'timeout')
    const init = resolveInit('LFE 0911')
    expect(init).toMatchObject({ method: 'POST', body: '{"account":"LFE 0911"}' })
    expect(timeout).toHaveBeenCalledWith(RESOLVE_TIMEOUT_MS)
    expect(init.signal).toBe(timeout.mock.results[0]?.value)
    expect(RESOLVE_TIMEOUT_MS).toBe(90_000)
  })
})
