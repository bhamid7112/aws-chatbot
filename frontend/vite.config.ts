import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies /api so development is same-origin, exactly as
// production is behind Caddy. The frontend therefore never needs an API base URL
// in any environment: it always posts to a relative path.
//
// The default target is the local Caddy container, not the API directly. The API
// deliberately publishes no host port — Caddy is the only way in, on a developer
// machine as on the server — so a dev request now takes the same route through
// the edge that a real one does, and the proxy and reverse proxy are exercised
// together rather than one of them only in production.
//
// Requires the compose stack to be up: `docker compose up -d` in deploy/.
// Override with DEV_API_TARGET when the API is somewhere else, such as
// `http://api:8000` on the container network in the compose dev stack.
const DEV_API_TARGET = process.env.DEV_API_TARGET ?? 'http://localhost'

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
