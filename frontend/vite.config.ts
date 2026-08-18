import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /api to the backend so development is same-origin,
// exactly as production is behind Caddy. The frontend therefore never needs an
// API base URL, in any environment: it always posts to a relative path.
//
// DEV_API_TARGET exists for the compose dev stack, where the API is reachable as
// `api:8000` on the container network rather than on localhost.
const DEV_API_TARGET = process.env.DEV_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: DEV_API_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    // Served by Caddy from /srv/www, with index.html as the SPA fallback.
    outDir: 'dist',
    sourcemap: false,
  },
})
