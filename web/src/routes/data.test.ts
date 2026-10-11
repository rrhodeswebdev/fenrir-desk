import { describe, expect, it } from 'vitest'
import { type Overview, gapText, pollMs, rollRows } from './data'

const rolls: Overview['rolls'] = [
  { series: 'ES', start: '2026-01-01', contract: 'ESH26' },
  { series: 'ES', start: '2026-03-12', contract: 'ESM26' },
  { series: 'ES', start: '2026-06-11', contract: 'ESU26' },
  { series: 'ES', start: '2026-09-10', contract: 'ESZ26' },
  { series: 'NQ', start: '2026-01-01', contract: 'NQH26' },
  { series: 'NQ', start: '2026-03-12', contract: 'NQM26' },
  { series: 'NQ', start: '2026-06-11', contract: 'NQU26' },
  { series: 'NQ', start: '2026-09-10', contract: 'NQZ26' },
]

describe('rollRows', () => {
  it('skips the synthetic first roll per series and puts ES and NQ on one row per date', () => {
    expect(rollRows(rolls, { ES: null, NQ: null })).toEqual([
      { start: '2026-03-12', contracts: { ES: 'ESM26', NQ: 'NQM26' }, mark: null },
      { start: '2026-06-11', contracts: { ES: 'ESU26', NQ: 'NQU26' }, mark: null },
      { start: '2026-09-10', contracts: { ES: 'ESZ26', NQ: 'NQZ26' }, mark: null },
    ])
  })

  it('marks the front month and the next roll', () => {
    expect(rollRows(rolls, { ES: 'ESM26', NQ: 'NQM26' }).map((r) => r.mark)).toEqual(['front', 'next', null])
  })

  it('has no next roll after the last calendar row', () => {
    expect(rollRows(rolls, { ES: 'ESZ26', NQ: 'NQZ26' }).map((r) => r.mark)).toEqual([null, null, 'front'])
  })
})

describe('pollMs', () => {
  const at = (state: Overview['pull']['state']) =>
    ({ pull: { state, started_at: null, finished_at: null, returncode: null, log_tail: [] } }) as unknown as Overview

  it('polls every 2 s only while a pull runs', () => {
    expect(pollMs(at('running'))).toBe(2000)
    expect(pollMs(at('idle'))).toBe(false)
    expect(pollMs(at('done'))).toBe(false)
    expect(pollMs(at('error'))).toBe(false)
    expect(pollMs(undefined)).toBe(false)
  })
})

describe('gapText', () => {
  it('lists single days and ranges', () => {
    expect(gapText([])).toBe('None')
    expect(gapText([['2024-03-29', '2024-03-29'], ['2025-01-02', '2025-01-03']])).toBe('2024-03-29, 2025-01-02 → 2025-01-03')
  })
})
