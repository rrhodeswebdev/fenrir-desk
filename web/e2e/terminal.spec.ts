import { expect, test } from '@playwright/test'

// Set by global-setup.ts after it opened the launch link.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

test('the Workbench terminal runs a real shell until it exits', async ({ page }) => {
  await page.goto('/')
  const term = page.getByTestId('terminal')
  await term.click()
  await page.keyboard.type('echo fenrir-$((40+2))\n') // only the output, not the echoed command, reads fenrir-42
  await expect(term).toContainText('fenrir-42')
  await page.keyboard.type('exit\n')
  await expect(term).toContainText('Disconnected: the shell exited.')
})

test('the Workbench has the latest backtest region', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('region', { name: 'Latest backtest' })).toBeVisible()
})
