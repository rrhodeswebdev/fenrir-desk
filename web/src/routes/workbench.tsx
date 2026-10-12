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

/** The line written when the socket closes. 1000 is the shell exiting; 1006 (no close frame) is a refused handshake or a lost server; the server names other causes in the reason. */
export const closeLine = (code: number, reason: string): string => {
  if (code === 1000) return 'Disconnected: the shell exited.'
  if (code === 1006)
    return 'Disconnected: the connection closed or was refused (session expired or server stopped). Reload to start a new shell.'
  return `Disconnected: ${reason || `code ${code}`}.`
}

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
    // keys typed while connecting wait here; the PTY holds them until the shell reads
    const queued: string[] = []
    const send = (frame: string) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(frame)
      else if (ws.readyState === WebSocket.CONNECTING) queued.push(frame)
    }
    ws.onopen = () => {
      ws.send(resizeFrame(term.cols, term.rows)) // the PTY starts at 80x24
      queued.splice(0).forEach((frame) => ws.send(frame))
    }
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
  return <div ref={ref} role="group" aria-label="Terminal" data-testid="terminal" className="h-full" />
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
