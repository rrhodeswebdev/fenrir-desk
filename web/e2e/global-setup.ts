import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import { cp, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { PORT } from '../playwright.config'

const repo = path.resolve(import.meta.dirname, '../..')
const LAUNCH = /^Open (http:\/\/localhost:\d+\/launch\?code=\S+)$/m

/** Starts `fenrir serve`, opens the launch link it prints, and saves the signed-in cookie for the tests. */
export default async function globalSetup() {
  const tmp = await mkdtemp(path.join(tmpdir(), 'fenrir-e2e-'))
  const dir = (name: string) => path.join(tmp, name)
  await cp(path.join(repo, 'rules'), dir('rules'), { recursive: true })
  await cp(path.join(repo, 'strategies'), dir('strategies'), { recursive: true })
  await writeFile(dir('empty.env'), '')
  const server = spawn('uv', ['run', 'fenrir', 'serve', '--port', String(PORT), '--static-dir', 'web/dist'], {
    cwd: repo,
    env: {
      ...process.env,
      FENRIR_DATA_DIR: dir('data'),
      FENRIR_RULES_DIR: dir('rules'),
      FENRIR_STRATEGIES_DIR: dir('strategies'),
      FENRIR_PRIVATE_DIR: dir('private'),
      FENRIR_HOLDOUT_MONTHS: '12',
      // never read the repo .env; the dummies also cover a secret exported in the shell
      FENRIR_ENV_FILE: dir('empty.env'),
      FENRIR_LSE_API_KEY: 'e2e-disabled',
      FENRIR_NT_TOKEN: 'e2e-disabled',
      FENRIR_NT_URL: 'http://127.0.0.1:9',
    },
    // own process group, so teardown stops uv and the python child together
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const exited = new Promise((resolve) => server.once('exit', resolve))
  const kill = (signal: NodeJS.Signals) => {
    try {
      process.kill(-server.pid!, signal)
    } catch {} // the group already exited
  }
  const stop = async () => {
    if (server.pid !== undefined && server.exitCode === null && server.signalCode === null) {
      kill('SIGTERM')
      const done = await Promise.race([exited.then(() => true), sleep(5_000, false, { ref: false })])
      if (!done) {
        kill('SIGKILL')
        await exited
      }
    }
    await rm(tmp, { recursive: true, force: true })
  }

  try {
    let timer: NodeJS.Timeout | undefined
    const launch = await new Promise<string>((resolve, reject) => {
      let out = ''
      let found = false
      // stays attached after the link is found, so the pipes keep draining
      const read = (chunk: Buffer) => {
        if (found) return
        out += chunk
        const link = out.match(LAUNCH)?.[1]
        if (link) {
          found = true
          resolve(link)
        }
      }
      server.stdout.on('data', read)
      server.stderr.on('data', read)
      server.on('error', reject) // e.g. uv not installed
      server.once('exit', (code) => reject(new Error(`fenrir serve exited (${code}) before its launch link:\n${out}`)))
      timer = setTimeout(() => reject(new Error(`no launch link from fenrir serve in 60s:\n${out}`)), 60_000)
    }).finally(() => clearTimeout(timer))

    const browser = await chromium.launch()
    try {
      const page = await browser.newPage()
      await page.goto(launch)
      if (new URL(page.url()).pathname !== '/') throw new Error(`launch link landed on ${page.url()}`)
      process.env.FENRIR_E2E_STATE = dir('state.json') // workers inherit the runner's env
      process.env.FENRIR_E2E_ENV_FILE = dir('empty.env') // the secrets file the Settings page writes
      await page.context().storageState({ path: process.env.FENRIR_E2E_STATE })
    } finally {
      await browser.close()
    }
  } catch (e) {
    await stop()
    throw e
  }
  return stop // Playwright runs a returned function as the global teardown
}
