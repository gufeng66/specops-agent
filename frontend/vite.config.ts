import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 开发时把 /api 转到 FastAPI。timeout:0 是因为 SSE 会挂很久。
const apiTarget = process.env.VITE_API_PROXY || 'http://127.0.0.1:8001'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        timeout: 0,
        proxyTimeout: 0,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('Accept-Encoding', 'identity')
          })
        },
      },
    },
  },
  test: {
    environment: 'happy-dom',
    include: ['src/**/*.spec.ts'],
  },
})
