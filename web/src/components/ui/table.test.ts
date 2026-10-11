import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { TableHead, TableRow } from './table'

describe('Table', () => {
  it('has no fills: rows are hairlines, hover takes the tonal step', () => {
    const row = renderToStaticMarkup(createElement(TableRow))
    expect(row).not.toMatch(/bg-muted|rounded/)
    expect(row).toContain('hover:bg-accent')
    expect(row).toContain('data-[state=selected]:bg-accent')
  })

  it('sets heads in Label type', () => {
    expect(renderToStaticMarkup(createElement(TableHead))).toMatch(/class="label /)
  })
})
