import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from '@playwright/test'

const backend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../backend')
const python = path.join(
  backend,
  '.venv',
  process.platform === 'win32' ? 'Scripts' : 'bin',
  process.platform === 'win32' ? 'python.exe' : 'python',
)

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  use: { baseURL: 'http://127.0.0.1:5173' },
  webServer: [
    {
      command: `"${python}" -m uvicorn app.main:app --port 8001`,
      cwd: backend,
      env: {
        ...process.env,
        LLM_PROVIDER: 'none',
        DEEPSEEK_API_KEY: '',
        DATABASE_PATH: './e2e-specops.db',
        CHECKPOINT_PATH: './e2e-checkpoints.db',
      },
      url: 'http://127.0.0.1:8001/health',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
})
