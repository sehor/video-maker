import { defineConfig } from '@playwright/test'

if (process.env.E2E_MANAGED !== '1' || !process.env.E2E_BASE_URL) {
  throw new Error('Use pnpm test:e2e to provision isolated native test servers and data')
}

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: process.env.E2E_BASE_URL, trace: 'retain-on-failure' },
  retries: 0,
  forbidOnly: true,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]]
})
