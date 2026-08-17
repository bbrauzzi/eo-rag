import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server proxies the API rather than calling it cross-origin, which is why the
// backend installs no CORS middleware: in development the browser sees one origin here,
// and in production FastAPI serves this build itself from the same port.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/ask': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/preview': 'http://localhost:8000',
      // Without this the dev server answers /items/… with the SPA fallback — index.html
      // under a 200, which fails as "Unexpected token '<'" rather than as a 404.
      '/items': 'http://localhost:8000',
    },
  },
})
