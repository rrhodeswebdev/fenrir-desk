import { expect, test } from '@playwright/test'

// Set by global-setup.ts after it opened the launch link.
test.use({ storageState: process.env.FENRIR_E2E_STATE })

test('the Workbench terminal runs a real shell until it exits', async ({ page }) => {
  await page.goto('/')
  const term = page.getByTestId('terminal')
  await term.click()
  // typed at once: keys sent while the socket connects must not be lost
  await page.keyboard.type('echo fenrir-$((40+2))\n')
  // a whole row of exactly fenrir-42: not the echoed command, not "sh: fenrir-42: command not found"
  await expect(term.locator('.xterm-rows > div', { hasText: /^fenrir-42\s*$/ })).toHaveCount(1)
  await page.keyboard.type('exit\n')
  await expect(term).toContainText('Disconnected: the shell exited.')
})

test('the Workbench has the latest backtest region', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('region', { name: 'Latest backtest' })).toBeVisible()
})
