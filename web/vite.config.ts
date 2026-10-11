/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { defineConfig } from 'vite'

const server = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, 'src') } },
  // Playwright specs in e2e/ are not Vitest tests.
  test: { include: ['src/**/*.test.{ts,tsx}'] },
  // Port is fixed: the server's dev Origin allowlist expects 5173.
  server: {
    port: 5173,
    strictPort: true,
    // The rail's mark is imported from the brand source; nothing else outside web/ is served.
    fs: { allow: ['.', '../assets/brand'] },
    proxy: {
      '/api': server,
      '/launch': server,
      '/ws': { target: server, ws: true },
    },
  },
})
