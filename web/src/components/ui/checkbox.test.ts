import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { Checkbox } from './checkbox'

describe('Checkbox', () => {
  it('is a square outline with no fill, shadow or ring, so the base focus outline shows', () => {
    const html = renderToStaticMarkup(createElement(Checkbox, { checked: true }))
    expect(html).toContain('role="checkbox"')
    expect(html).toContain('border-input')
    expect(html).not.toMatch(/rounded|shadow|ring-|outline-none|bg-primary/)
  })
})
