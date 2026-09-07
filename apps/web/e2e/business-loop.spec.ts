import { expect, test } from '@playwright/test'

test('register, bind an image and recover simulated I2V generation', async ({ page }) => {
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
  await page.getByLabel('上传参考素材').setInputFiles({ name: 'reference.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5Z8AAAAASUVORK5CYII=', 'base64') })
  await expect(page.getByRole('list', { name: '已有素材' }).getByText('reference.png', { exact: false })).toBeVisible()
  await page.getByLabel('镜头名称').fill('E2E 镜头')
  await page.getByLabel('提示词').fill('电影感的雨夜城市街道')
  await page.getByRole('button', { name: '创建镜头' }).click()
  await expect(page.getByRole('button', { name: '开始生成' })).toBeDisabled()
  await page.getByLabel('首帧参考图（必选）').selectOption({ label: 'reference.png' })
  const selectedAsset = await page.getByLabel('首帧参考图（必选）').inputValue()
  await page.route('**/v1/shots/*/input', route => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: '绑定暂时失败' } })
  }), { times: 1 })
  await page.getByRole('button', { name: '保存参考图' }).click()
  await expect(page.getByText('绑定暂时失败', { exact: true })).toBeVisible()
  await expect(page.getByText('当前参考图：未绑定')).toBeVisible()
  const binding = page.waitForResponse(response => response.url().endsWith('/input') && response.request().method() === 'PUT')
  await page.getByRole('button', { name: '保存参考图' }).click()
  expect((await (await binding).json()).references[0].asset_id).toBe(selectedAsset)
  await expect(page.getByText('当前参考图：reference.png')).toBeVisible()
  await page.reload()
  await expect(page.getByText('当前参考图：reference.png')).toBeVisible()
  await page.getByRole('button', { name: '领取 10 秒测试额度' }).click()
  await expect(page.getByText('FAST 可用：10 秒')).toBeVisible()
  const acceptedJobs: string[] = []
  const keys: string[] = []
  await page.route('**/v1/generations', async (route) => {
    const response = await route.fetch()
    expect(response.status()).toBe(202)
    acceptedJobs.push((await response.json()).id)
    keys.push(route.request().headers()['idempotency-key']!)
    if (acceptedJobs.length === 1) await route.abort('failed')
    else await route.fulfill({ response })
  })
  await page.getByRole('button', { name: '开始生成' }).click()
  await expect(page.getByRole('button', { name: '恢复原请求' })).toBeVisible()
  await page.reload()
  let failedPoll = false
  let failedDownload = false
  await page.route('**/v1/generations/*', async (route) => {
    if (route.request().method() === 'GET' && !failedPoll) {
      failedPoll = true
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{}' })
    } else await route.continue()
  })
  await page.route('**/v1/outputs/*/content', async (route) => {
    if (!failedDownload) { failedDownload = true; await route.abort('failed') }
    else await route.continue()
  })
  await page.getByRole('button', { name: '恢复原请求' }).click()
  await expect(page.getByText('SUCCEEDED', { exact: true })).toBeVisible({ timeout: 20_000 })
  await page.getByRole('button', { name: '重试下载' }).click()
  expect(failedPoll).toBe(true)
  expect(failedDownload).toBe(true)
  await expect(page.locator('video')).toBeVisible()
  expect(acceptedJobs).toHaveLength(2)
  expect(new Set(acceptedJobs).size).toBe(1)
  expect(new Set(keys).size).toBe(1)
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
