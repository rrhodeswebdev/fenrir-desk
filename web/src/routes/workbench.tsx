import { createRoute } from '@tanstack/react-router'
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { useEffect, useRef } from 'react'
import { wsUrl } from '@/api'
import { Page } from '@/components/page'
import { rootRoute } from './root'

// WS /ws/terminal (docs/contracts.md): text frames in, binary PTY output back.
export const inputFrame = (data: string): string => JSON.stringify({ type: 'input', data })
export const resizeFrame = (cols: number, rows: number): string => JSON.stringify({ type: 'resize', cols, rows })

/** The line written when the socket closes. 1000 is the shell exiting; the server names other causes in the reason. */
export const closeLine = (code: number, reason: string): string =>
  code === 1000 ? 'Disconnected: the shell exited.' : `Disconnected: ${reason || `code ${code}`}.`

const token = (name: string): string => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

function TerminalPane() {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current!
    const term = new Terminal({
      fontFamily: getComputedStyle(el).fontFamily,
      fontSize: 13,
      theme: {
        background: token('--ground'),
        foreground: token('--ink-2'),
        cursor: token('--ink'),
        cursorAccent: token('--ground'),
        selectionBackground: token('--ember'),
        selectionForeground: token('--ground'),
        scrollbarSliderBackground: token('--rule-strong'),
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(el)
    fit.fit()
    const ws = new WebSocket(wsUrl('/ws/terminal'))
    ws.binaryType = 'arraybuffer'
    const send = (frame: string) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(frame)
    }
    ws.onopen = () => send(resizeFrame(term.cols, term.rows)) // the PTY starts at 80x24
    ws.onmessage = (e: MessageEvent<ArrayBuffer>) => term.write(new Uint8Array(e.data))
    // shortcut: no reconnect; a reload starts a new shell.
    ws.onclose = (e) => term.write(`\r\n${closeLine(e.code, e.reason)}\r\n`)
    term.onData((data) => send(inputFrame(data)))
    term.onResize(({ cols, rows }) => send(resizeFrame(cols, rows)))
    const resize = new ResizeObserver(() => fit.fit())
    resize.observe(el)
    term.focus()
    return () => {
      resize.disconnect()
      ws.onclose = null // StrictMode: never write to the disposed terminal
      ws.close()
      term.dispose()
    }
  }, [])
  // no padding here: the fit add-on sizes the terminal to this box
  return <div ref={ref} data-testid="terminal" className="h-full" />
}

export const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: () => (
    <Page
      title="Workbench"
      className="grid h-[calc(100vh-72px)] grid-cols-[minmax(0,1fr)_360px] max-md:h-auto max-md:grid-cols-1"
    >
      <div className="min-h-0 p-4 max-md:h-[60vh]">
        <TerminalPane />
      </div>
      <section aria-labelledby="latest-backtest" className="border-l p-8 max-md:border-t max-md:border-l-0 max-md:px-4">
        <h2 id="latest-backtest">Latest backtest</h2>
        <p className="mt-4 max-w-[44ch] text-ink-2">Not built yet. The latest report lands here.</p>
      </section>
    </Page>
  ),
})
