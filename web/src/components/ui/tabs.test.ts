import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './tabs'

describe('Tabs', () => {
  it('has no fill, radius, shadow or ring on the triggers, so the base focus outline shows', () => {
    const html = renderToStaticMarkup(
      createElement(
        Tabs,
        { defaultValue: 'a' },
        createElement(TabsList, null, createElement(TabsTrigger, { value: 'a' }, 'A'), createElement(TabsTrigger, { value: 'b' }, 'B')),
        createElement(TabsContent, { value: 'a' }, 'body'),
      ),
    )
    expect(html).toContain('role="tab" aria-selected="true"')
    expect(html).toContain('data-[state=active]:after:bg-ember')
    expect(html).not.toMatch(/rounded|shadow-sm|ring-|outline-none|bg-muted|bg-background/)
  })
})
