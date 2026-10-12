import { describe, expect, it } from 'vitest'
import { bins, chartOptions, equityCaption, equitySeries, histogramText } from './charts'

describe('bins', () => {
  it('fills the days between bins with zero', () => {
    expect(bins([[2, 4], [5, 1]])).toEqual([[2, 4], [3, 0], [4, 0], [5, 1]])
  })

  it('is empty when nothing passed', () => {
    expect(bins([])).toEqual([])
  })
})

describe('histogramText', () => {
  it('spells out each bin', () => {
    expect(histogramText([[1, 4], [2, 11]])).toBe('1 day: 4, 2 days: 11')
  })
})

describe('equitySeries', () => {
  it('turns ISO timestamps into UTC seconds', () => {
    expect(equitySeries([['2023-05-01T20:59:00Z', -120.5]])).toEqual([{ time: 1682974740, value: -120.5 }])
  })
})

describe('equityCaption', () => {
  it('names the start, outcome, length, last value and range', () => {
    const points: [string, number][] = [
      ['2023-05-01T20:59:00Z', -300],
      ['2023-05-02T20:59:00Z', 1400],
      ['2023-05-03T20:59:00Z', 1250],
    ]
    expect(equityCaption({ start: '2023-05-01', outcome: 'payout', points })).toBe(
      '2023-05-01 · payout · 3 days · end $1,250.00 · low −$300.00 · high $1,400.00',
    )
  })

  it('says so when the attempt closed no day', () => {
    expect(equityCaption({ start: '2023-05-01', outcome: 'breached', points: [] })).toBe('2023-05-01 · breached · no closed day')
  })
})

describe('chartOptions', () => {
  it('colours the chart from the design tokens', () => {
    const tokens: Record<string, string> = { '--ground': '#0E1116', '--chart-3': '#8FA0B2', '--rule': '#262D37' }
    const o = chartOptions((name) => tokens[name] ?? '', 'IBM Plex Mono')
    expect(o.layout).toMatchObject({ background: { color: '#0E1116' }, textColor: '#8FA0B2', fontFamily: 'IBM Plex Mono' })
    expect(o.grid?.horzLines?.color).toBe('#262D37')
  })
})
