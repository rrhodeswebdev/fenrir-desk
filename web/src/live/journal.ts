// Pure journal helpers for the tape and the full journal: `GET /api/live/journal?account=&day=` rows as lines
// (DESIGN.md "Journal Tape": kind in tracked caps, Slate by default, Ember for HALT and ALERT, Red for MISMATCH).
import { ApiError } from '@/api'
import type { Tone } from '@/components/state-text'
import { nyTime, price, usd } from './model'

export type JournalRow = {
  id: number
  ts: string
  account: string
  kind: 'bar' | 'action' | 'rejection' | 'fill' | 'alert' | 'halt' | 'peak'
  payload: Record<string, unknown>
}

/** The trading day of `now` as the server counts it (`core.sessions.trading_day`): New York date six hours on,
 * so from 18:00 it is the next day. */
const nyDate = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' })
export const tradingDay = (now: number): string => nyDate.format(now + 6 * 3600_000)

/** Poll every 5 s and keep polling through errors; a 401 will not fix itself. */
export const journalPollMs = ({ error }: { error: unknown }): number | false =>
  error instanceof ApiError && error.status === 401 ? false : 5000

/** Every account's rows in one list, newest first (ts, then id). */
export const merge = (lists: JournalRow[][]): JournalRow[] =>
  lists.flat().sort((x, y) => (x.ts < y.ts ? 1 : x.ts > y.ts ? -1 : y.id - x.id))

/** The tape: the latest events, without the per-minute `bar` rows and the `peak` bookkeeping (the full journal has them). */
export const TAPE_ROWS = 12
export const tape = (rows: JournalRow[]): JournalRow[] => rows.filter((r) => r.kind !== 'bar' && r.kind !== 'peak').slice(0, TAPE_ROWS)

export type Line = { key: string; time: string; kind: string; tone: Tone; text: string }

const num = (v: unknown): string => (typeof v === 'number' ? price(v) : String(v ?? '—'))
const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v))

/** An action payload (`{type, ...fields}`) as words. */
function actionText(p: Record<string, unknown>): string {
  if (p.type === 'Bracket') return `Bracket ${str(p.side)} ${str(p.qty)} · stop ${num(p.stop)} · target ${num(p.target)}`
  if (p.type === 'Cancel') return `Cancel ${str(p.order_id)}`
  return str(p.type) || 'Action'
}

const join = (...parts: (string | false | null | undefined)[]) => parts.filter(Boolean).join(' · ')

/** One journal row as a tape line: New York time, the kind in caps with its tone, then `<account> · <what happened>`. */
export function journalLine(r: JournalRow): Line {
  const p = r.payload
  let kind = r.kind.toUpperCase()
  let tone: Tone = 'idle'
  let text: string
  switch (r.kind) {
    case 'bar':
      text = join(`${str(p.contract)} C ${num(p.close)}`, p.actions ? `${str(p.actions)} action${p.actions === 1 ? '' : 's'}` : 'hold')
      break
    case 'action':
      text = join(actionText(p), p.mode === 'dry' ? 'dry run' : !p.sent && 'not sent', str(p.reason))
      break
    case 'rejection':
      kind = 'REJECT' // the guard worked: Slate, no action needed
      text = join(typeof p.action === 'object' && p.action ? actionText(p.action as Record<string, unknown>) : 'Action', str(p.reason))
      break
    case 'fill': {
      const qty = typeof p.qty === 'number' ? p.qty : 0
      text = join(`${qty < 0 ? 'Sell' : 'Buy'} ${Math.abs(qty)} ${str(p.instrument)} @ ${num(p.price)}`)
      break
    }
    case 'alert':
      tone = 'caution'
      text = str(p.message)
      break
    case 'halt':
      if (p.kind === 'reconcile') {
        kind = 'MISMATCH'
        tone = 'alarm'
      } else if (p.kind === 'resolve') {
        kind = 'RESOLVE' // lifts the pauses: nothing to act on
      } else tone = 'caution'
      text = join(str(p.reason), typeof p.until === 'string' && `until ${p.until}`)
      break
    case 'peak':
      text = `peak ${typeof p.peak === 'number' ? usd(p.peak) : '—'}`
      break
    default:
      text = JSON.stringify(p)
  }
  return { key: `${r.account}-${r.id}`, time: nyTime(Date.parse(r.ts)), kind, tone, text: join(r.account, text) }
}
