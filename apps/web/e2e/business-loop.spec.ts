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
  await page.getByRole('button', { name: '开始生成' }).click()
  await expect(page.getByText('SUCCEEDED', { exact: true })).toBeVisible({ timeout: 20_000 })
  await expect(page.locator('video')).toBeVisible()
})
