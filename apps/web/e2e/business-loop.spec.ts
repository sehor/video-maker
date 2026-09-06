import { expect, test } from '@playwright/test'

test('register, create project, create shot and run mock generation', async ({ page }) => {
  const email = `e2e-${Date.now()}@example.test`
  await page.goto('/login')
  await page.getByRole('button', { name: '没有账号？创建一个' }).click()
  await page.getByLabel('名称').fill('E2E 用户')
  await page.getByLabel('邮箱').fill(email)
  await page.getByLabel('密码').fill('test-password-123')
  await page.getByRole('button', { name: '注册并进入' }).click()
  await expect(page).toHaveURL(/projects/)
  await page.getByRole('button', { name: '新建项目' }).click()
  await page.getByLabel('项目名称').fill('E2E 项目')
  await page.getByRole('button', { name: '创建项目' }).click()
  await page.getByLabel('镜头名称').fill('E2E 镜头')
  await page.getByLabel('提示词').fill('电影感的雨夜城市街道')
  await page.getByRole('button', { name: '创建镜头' }).click()
  await page.getByRole('button', { name: '领取 10 秒测试额度' }).click()
  await expect(page.getByText('FAST 可用：10 秒')).toBeVisible()
  await page.getByRole('button', { name: '开始生成' }).click()
  await expect(page.getByText('SUCCEEDED', { exact: true })).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('video')).toBeVisible()
  await expect.poll(() => page.locator('video').evaluate((video: HTMLVideoElement) => ({
    ready: video.readyState >= 2, width: video.videoWidth, height: video.videoHeight,
    error: video.error?.code ?? null
  }))).toEqual({ ready: true, width: 1280, height: 720, error: null })
  // Reload verifies persistence and output retrieval, not just an optimistic UI state.
  await page.reload()
  await expect(page.getByText('SUCCEEDED', { exact: true })).toBeVisible()
  await expect(page.locator('video')).toBeVisible()
})

test('protected project list redirects an unauthenticated visitor to login', async ({ page }) => {
  await page.goto('/projects')
  await expect(page).toHaveURL(/\/login/)
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible()
})
