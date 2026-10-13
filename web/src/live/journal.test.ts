import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api'
import { deskJournal, row } from './fixtures'
import { TAPE_ROWS, journalLine, journalPollMs, merge, tape, tradingDay } from './journal'

describe('trading day', () => {
  it('is the New York date, the next one from 18:00', () => {
    expect(tradingDay(Date.parse('2026-10-13T21:59:00Z'))).toBe('2026-10-13') // 17:59 EDT
    expect(tradingDay(Date.parse('2026-10-13T22:00:00Z'))).toBe('2026-10-14') // 18:00 EDT
    expect(tradingDay(Date.parse('2026-10-14T03:00:00Z'))).toBe('2026-10-14') // 23:00 EDT
  })
})

describe('tape', () => {
  it('merges every account newest first and leaves bars and peaks to the full journal', () => {
    const all = merge(Object.values(deskJournal()))
    expect(all).toHaveLength(9)
    expect(all[0]?.kind).toBe('peak') // 18:31:00, the later id first
    const t = tape(all)
    expect(t.map((r) => r.kind)).toEqual(['halt', 'fill', 'action', 'halt', 'fill', 'rejection', 'alert'])
    const many = Array.from({ length: 30 }, (_, i) => row('A', `2026-10-13T15:${String(i).padStart(2, '0')}:00+00:00`, 'alert', { message: 'x' }))
    expect(tape(merge([many]))).toHaveLength(TAPE_ROWS)
  })

  it('polls every 5 s, not after a 401', () => {
    expect(journalPollMs({ error: null })).toBe(5000)
    expect(journalPollMs({ error: new ApiError(500, '') })).toBe(5000)
    expect(journalPollMs({ error: new ApiError(401, '') })).toBe(false)
  })
})

describe('journal line', () => {
  const line = (kind: Parameters<typeof row>[2], payload: Record<string, unknown>) =>
    journalLine(row('LFE 0911', '2026-10-13T18:30:12+00:00', kind, payload))

  it('a reconcile halt is MISMATCH in Red', () => {
    expect(line('halt', { reason: 'position mismatch: NinjaTrader long 1, runner flat', kind: 'reconcile', until: null })).toMatchObject({
      time: '14:30:12',
      kind: 'MISMATCH',
      tone: 'alarm',
      text: 'LFE 0911 · position mismatch: NinjaTrader long 1, runner flat',
    })
  })

  it('other halts and alerts are Ember; a resolve and a rejection stay Slate', () => {
    expect(line('halt', { reason: 'safety margin', kind: 'safety', until: '2026-10-13' })).toMatchObject({
      kind: 'HALT',
      tone: 'caution',
      text: 'LFE 0911 · safety margin · until 2026-10-13',
    })
    expect(line('halt', { reason: 'disarmed', kind: null, until: null })).toMatchObject({ kind: 'HALT', tone: 'caution' })
    expect(line('halt', { reason: 'resolved: drift', kind: 'resolve', until: null })).toMatchObject({ kind: 'RESOLVE', tone: 'idle' })
    expect(line('alert', { message: 'NinjaTrader link lost' })).toMatchObject({ kind: 'ALERT', tone: 'caution', text: 'LFE 0911 · NinjaTrader link lost' })
    expect(
      line('rejection', { action: { type: 'Bracket', side: 'long', qty: 4, stop: 6801, target: 6830 }, reason: 'stop-out would breach' }),
    ).toMatchObject({ kind: 'REJECT', tone: 'idle', text: 'LFE 0911 · Bracket long 4 · stop 6,801.00 · target 6,830.00 · stop-out would breach' })
  })

  it('bars, actions, fills and peaks in words', () => {
    expect(line('bar', { contract: 'MNQZ26', close: 25418.5, actions: 0 }).text).toBe('LFE 0911 · MNQZ26 C 25,418.50 · hold')
    expect(line('bar', { contract: 'MNQZ26', close: 25418.5, actions: 2 }).text).toBe('LFE 0911 · MNQZ26 C 25,418.50 · 2 actions')
    expect(line('action', { type: 'Flatten', mode: 'sim', sent: true, client_id: 'c', reason: 'flat by 16:40' }).text).toBe(
      'LFE 0911 · Flatten · flat by 16:40',
    )
    expect(line('action', { type: 'Bracket', side: 'short', qty: 1, stop: 2, target: 1, mode: 'dry', sent: false }).text).toBe(
      'LFE 0911 · Bracket short 1 · stop 2.00 · target 1.00 · dry run',
    )
    expect(line('action', { type: 'Flatten', mode: 'live', sent: false, reason: 'already exiting' }).text).toBe(
      'LFE 0911 · Flatten · not sent · already exiting',
    )
    expect(line('fill', { qty: -2, instrument: 'MNQ 12-26', price: 25301.75 }).text).toBe('LFE 0911 · Sell 2 MNQ 12-26 @ 25,301.75')
    expect(line('peak', { peak: 51412 }).text).toBe('LFE 0911 · peak $51,412')
  })
})
