import { expect, test } from '@playwright/test'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'

// Set by global-setup.ts: the server's secrets file is a temp file, never the repo .env.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

const repoEnv = path.resolve(import.meta.dirname, '../../.env')
const read = () => (existsSync(repoEnv) ? readFileSync(repoEnv, 'utf8') : null)
const before = read()
test.afterAll(() => expect(read(), 'the repo .env changed').toBe(before))

test('a setting saves to the temp secrets file, survives a reload, and a bad value shows its error under the input', async ({ page }) => {
  await page.goto('/settings')
  const form = page.getByRole('region', { name: 'Secrets and limits' })
  const save = form.getByRole('button', { name: 'Save' })
  const stale = form.getByLabel('Stale threshold')
  await expect(form.getByLabel('Safety margin')).toHaveAccessibleName('Safety margin % of the daily loss limit or of the drawdown room at session start')
  await expect(save).toBeDisabled()
  await stale.fill('45')
  await save.click()
  await expect(form.getByRole('status')).toHaveText('Saved.')
  expect(readFileSync(process.env.FENRIR_E2E_ENV_FILE!, 'utf8')).toBe('FENRIR_STALE_SECONDS=45\n')
  await page.reload()
  await expect(stale).toHaveValue('45')

  await stale.fill('0')
  await save.click()
  await expect(stale).toHaveAttribute('aria-invalid', 'true')
  await expect(stale).toHaveAccessibleDescription(/^Input should be greater than 0/)
  await expect(form.getByRole('alert')).toContainText('stale_seconds: Input should be greater than 0')
})

test('an account price and daily loss limit save and survive a reload', async ({ page }) => {
  await page.goto('/settings')
  const form = page.getByRole('region', { name: 'Accounts' })
  const price = form.getByLabel('lucid-flex-50k evaluation price')
  const dll = form.getByRole('switch', { name: 'lucid-flex-50k daily loss limit' })
  await price.fill('97.5')
  await dll.click()
  await form.getByRole('button', { name: 'Save' }).click()
  await expect(form.getByRole('status')).toHaveText('Saved.')
  await page.reload()
  await expect(price).toHaveValue('97.5')
  await expect(dll).toHaveAttribute('aria-checked', 'true')
})
