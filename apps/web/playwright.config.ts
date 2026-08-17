import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  use: { baseURL: process.env.E2E_BASE_URL || 'http://web:3000', trace: 'retain-on-failure' },
  retries: 1
})
