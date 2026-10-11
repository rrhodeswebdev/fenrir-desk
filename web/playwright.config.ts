import { defineConfig } from '@playwright/test'

export const PORT = Number(process.env.FENRIR_E2E_PORT ?? 8799)

export default defineConfig({
  testDir: './e2e',
  // Starts `fenrir serve` and signs in through its launch link; no webServer, which hides the link.
  globalSetup: './e2e/global-setup.ts',
  forbidOnly: !!process.env.CI,
  globalTimeout: 300_000,
  reporter: 'list',
  use: { baseURL: `http://localhost:${PORT}` },
})
