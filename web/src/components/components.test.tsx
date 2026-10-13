import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { Page } from './page'
import { StateText } from './state-text'

describe('StateText', () => {
  it.each([
    ['normal', 'text-ink-2'],
    ['idle', 'text-ink-3'],
    ['caution', 'text-ember'],
    ['alarm', 'text-red'],
  ] as const)('colours %s with %s', (tone, cls) => {
    expect(renderToStaticMarkup(<StateText tone={tone}>Halt</StateText>)).toMatch(new RegExp(`class="[^"]*\\b${cls}\\b[^"]*">Halt<`))
  })
})

describe('Page', () => {
  it('puts the title and strip in the top strip, then the body', () => {
    const html = renderToStaticMarkup(
      <Page title="Live" strip={<button>Kill</button>}>
        <p>body</p>
      </Page>,
    )
    expect(html).toMatch(/^<header [^>]*><h1>Live<\/h1><button>Kill<\/button><\/header><div class="p-8 max-md:px-4"><p>body<\/p><\/div>$/)
    // wraps at every width, so a strip wider than the column never pushes anything off-screen
    expect(html).toMatch(/^<header class="[^"]*\bflex-wrap\b/)
  })

  it('lets className replace the body padding', () => {
    expect(renderToStaticMarkup(<Page title="Workbench" className="p-0" />)).toContain('<div class="p-0">')
  })
})
