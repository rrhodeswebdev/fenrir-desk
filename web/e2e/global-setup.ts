import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { PORT } from '../playwright.config'

const repo = path.resolve(import.meta.dirname, '../..')

/** Starts `fenrir serve`, opens the launch link it prints, and saves the signed-in cookie for the tests. */
export default async function globalSetup() {
  const tmp = await mkdtemp(path.join(tmpdir(), 'fenrir-e2e-'))
  const server = spawn('uv', ['run', 'fenrir', 'serve', '--port', String(PORT), '--static-dir', 'web/dist'], {
    cwd: repo,
    env: {
      ...process.env,
      FENRIR_DATA_DIR: 'tests/fixtures/e2e/data',
      FENRIR_PRIVATE_DIR: path.join(tmp, 'private'),
      FENRIR_HOLDOUT_MONTHS: '12',
    },
    // own process group, so teardown stops uv and the python child together
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const stop = async () => {
    if (server.exitCode === null && server.signalCode === null) {
      const exited = new Promise((resolve) => server.once('exit', resolve))
      process.kill(-server.pid!, 'SIGTERM')
      await exited
    }
    await rm(tmp, { recursive: true, force: true })
  }

  try {
    const launch = await new Promise<string>((resolve, reject) => {
      let out = ''
      const read = (chunk: Buffer) => {
        out += chunk
        const link = out.match(/^Open (http:\/\/localhost:\d+\/launch\?code=\S+)$/m)?.[1]
        if (link) resolve(link)
      }
      server.stdout.on('data', read)
      server.stderr.on('data', read)
      server.once('exit', (code) => reject(new Error(`fenrir serve exited (${code}) before its launch link:\n${out}`)))
      setTimeout(() => reject(new Error(`no launch link from fenrir serve in 60s:\n${out}`)), 60_000)
    })

    const browser = await chromium.launch()
    const page = await browser.newPage()
    await page.goto(launch)
    if (new URL(page.url()).pathname !== '/') throw new Error(`launch link landed on ${page.url()}`)
    process.env.FENRIR_E2E_STATE = path.join(tmp, 'state.json') // workers inherit the runner's env
    await page.context().storageState({ path: process.env.FENRIR_E2E_STATE })
    await browser.close()
  } catch (e) {
    await stop()
    throw e
  }
  return stop // Playwright runs a returned function as the global teardown
}
