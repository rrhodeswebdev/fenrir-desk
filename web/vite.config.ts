import react from '@vitejs/plugin-react'
import path from 'node:path'
import { defineConfig } from 'vite'

const server = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, 'src') } },
  // Port is fixed: the server's dev Origin allowlist expects 5173.
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': server,
      '/launch': server,
      '/ws': { target: server, ws: true },
    },
  },
})
