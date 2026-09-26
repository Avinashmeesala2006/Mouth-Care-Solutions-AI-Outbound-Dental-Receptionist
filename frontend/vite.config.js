import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the UI runs on the Vite server and API calls use relative paths,
// so proxy them to FastAPI. Production is served same-origin (nginx or FastAPI).
const api = process.env.VITE_DEV_API_TARGET || 'http://127.0.0.1:8000'
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': api, '/health': api, '/ready': api } },
})
