import { type Page, expect, test } from '@playwright/test'
import { PORT } from '../playwright.config'
import { SESSION_EXPIRED } from '../src/errorText'

const pages = [
  ['/', 'Workbench'],
  ['/rules', 'Rules'],
  ['/data', 'Data'],
  ['/backtests', 'Backtests'],
  ['/strategies', 'Strategies'],
  ['/live', 'Live'],
  ['/settings', 'Settings'],
] as const

// Set by global-setup.ts after it opened the launch link.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

const rail = (page: Page) => page.getByRole('navigation', { name: 'Pages' })
const apiStatus = (page: Page) => page.evaluate(() => fetch('/api/rules').then((r) => r.status))

async function expectPage(page: Page, title: string, active: string) {
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(title)
  await expect(rail(page).locator('[aria-current="page"]')).toHaveText(active)
}

test('every page loads in the shell, marked in the rail', async ({ page }) => {
  for (const [path, name] of pages) {
    await page.goto(path)
    await expectPage(page, name, name)
  }
})

test('the rail moves between pages', async ({ page }) => {
  await page.goto('/settings') // so every click, Workbench first, changes page
  for (const [path, name] of pages) {
    await rail(page).getByRole('link', { name, exact: true }).click()
    await expect(page).toHaveURL(path)
    await expectPage(page, name, name)
  }
})

test('a deep link renders the report under Backtests', async ({ page }) => {
  await page.goto('/backtests/abc')
  await expectPage(page, 'Report', 'Backtests')
  await expect(page.getByText('abc', { exact: true })).toBeVisible()
})

test('the compare tabs switch between by account and by strategy', async ({ page }) => {
  await page.goto('/backtests')
  const tab = (name: string) => page.getByRole('tab', { name })
  await expect(tab('By account')).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('tabpanel')).toHaveText('No results yet.')
  await tab('By strategy').click()
  await expect(tab('By strategy')).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('tabpanel')).toHaveText('No results yet.')
})

test('an unknown path shows not found', async ({ page }) => {
  await page.goto('/no-such-page')
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Not found')
  await expect(rail(page)).toBeVisible()
})

test('the launch link signed the browser in', async ({ page }) => {
  await page.goto('/')
  expect(await apiStatus(page)).toBe(200)
})

// the server closes a socket without a valid session cookie or Origin before accepting it
test('/backtests opens the jobs socket on the same host and the server accepts it', async ({ page }) => {
  const socket = page.waitForEvent('websocket')
  await page.goto('/backtests')
  const ws = await socket
  expect(ws.url()).toBe(`ws://localhost:${PORT}/ws/jobs`)
  await page.waitForTimeout(1_000) // a refused socket closes at once
  expect(ws.isClosed()).toBe(false)
  await expect(page.getByText('Live updates stopped')).toHaveCount(0)
})

test.describe('without the launch link', () => {
  test.use({ storageState: { cookies: [], origins: [] } })

  // Static files are public; the page's 401 shows the "Session expired" line.
  test('the shell still loads but the API refuses', async ({ page }) => {
    await page.goto('/rules')
    await expectPage(page, 'Rules', 'Rules')
    await expect(page.getByRole('alert')).toHaveText(SESSION_EXPIRED)
    expect(await apiStatus(page)).toBe(401)
  })
})
