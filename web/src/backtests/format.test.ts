import { describe, expect, it } from 'vitest'
import { money, num, paramText, pct } from './format'

describe('format', () => {
  it('shows rates as a percent to one place', () => {
    expect(pct(0.31)).toBe('31.0%')
    expect(pct(0)).toBe('0.0%')
  })

  it('shows counts and medians grouped, a missing median as a dash', () => {
    expect(num(1700)).toBe('1,700')
    expect(num(9.5)).toBe('9.5')
    expect(num(null)).toBe('—')
  })

  it('shows signed dollars as Compare does, a missing median as a dash', () => {
    expect(money(87.5)).toBe('+$87.50')
    expect(money(-1250)).toBe('-$1,250.00')
    expect(money(0)).toBe('$0.00')
    expect(money(null)).toBe('—')
  })

  it('shows a param string as written and anything else as JSON', () => {
    expect(paramText('09:30')).toBe('09:30')
    expect(paramText(12)).toBe('12')
    expect(paramText([1, 2])).toBe('[1,2]')
  })
})
