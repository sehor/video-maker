<script setup lang="ts">
import type { Shot, Wallet } from '~/types/domain'
import { GenerationOperationController } from '~/utils/generation-operation'
import type { GenerationOperation } from '~/utils/generation-operation'

const route = useRoute()
const api = useApi()
const shot = ref<Shot | null>(null)
const busy = ref(false)
const grantBusy = ref(false)
const error = ref('')
const wallet = ref<Wallet | null>(null)
const { session } = useAuth()
const operation = ref<GenerationOperation | null>(null)
const controller = () => {
  if (!session.value?.user.id) throw new Error('请登录后恢复操作')
  const userId = session.value.user.id
  const scopedRequest: typeof api.request = async (path, options) => {
    if (session.value?.user.id !== userId) throw new Error('登录用户已变化，请重新登录后恢复原操作')
    return api.request(path, options)
  }
  return new GenerationOperationController(userId, String(route.params.shot_id), localStorage, scopedRequest)
}
const generate = async (startNew = false) => {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const userId = session.value?.user.id
    const task = controller()
    const run = async () => {
      if (startNew) task.startNew()
      return task.run()
    }
    const jobId = await navigator.locks.request(task.key, run)
    if (session.value?.user.id === userId) await navigateTo(`/jobs/${jobId}`)
  } catch (e) { error.value = (e as Error).message } finally {
    busy.value = false
    try { operation.value = controller().read() } catch { /* Keep the actionable recovery error. */ }
  }
}

const loadWallet = async () => { wallet.value = await api.request<Wallet>('/v1/wallet') }

const grantTestSeconds = async () => {
  grantBusy.value = true
  error.value = ''
  try {
    await api.request('/v1/wallet/test-grants', {
      method: 'POST',
      body: JSON.stringify({
        tier: 'FAST',
        amount_ms: 10_000,
        idempotency_key: `web-test-grant:${crypto.randomUUID()}`,
        reason: '开发环境测试额度'
      })
    })
    await loadWallet()
  } catch (e) { error.value = (e as Error).message } finally { grantBusy.value = false }
}

onMounted(async () => {
  try {
  operation.value = controller().read()
  const [loadedShot] = await Promise.all([
    api.request<Shot>(`/v1/shots/${route.params.shot_id}`),
    loadWallet()
  ])
  shot.value = loadedShot
  } catch (e) { error.value = (e as Error).message }
})
</script>

<template>
  <UAlert v-if="error && !shot" color="error" :description="error" />
  <div v-if="shot">
    <div class="page-head">
      <div><p class="muted">镜头</p><h1>{{ shot.title }}</h1></div>
      <NuxtLink :to="`/projects/${route.params.id}`"><UButton color="neutral" variant="soft">返回项目</UButton></NuxtLink>
    </div>
    <div class="two-col">
      <section class="panel">
        <h2>镜头参数</h2>
        <p>{{ shot.prompt }}</p>
        <div class="actions muted"><span>{{ shot.duration_seconds }} 秒</span><span>{{ shot.aspect_ratio }}</span></div>
      </section>
      <section class="panel form-stack">
        <div><h2>提交生成任务</h2><p class="muted">按当前质量档生成视频。</p></div>
        <div class="actions">
          <span role="status" class="muted">FAST 可用：{{ (wallet?.balances['FAST_MS']?.['USER_AVAILABLE'] || 0) / 1000 }} 秒</span>
          <UButton color="neutral" variant="soft" :loading="grantBusy" @click="grantTestSeconds">领取 10 秒测试额度</UButton>
        </div>
        <UAlert v-if="error" color="error" :description="error" />
        <p v-if="operation && !['complete', 'failed'].includes(operation.phase)" role="status" class="muted">上次操作结果尚未确认，请恢复原请求。</p>
        <UButton v-if="!operation || !['complete', 'failed'].includes(operation.phase)" :loading="busy" icon="i-lucide-play" @click="generate()">{{ operation ? '恢复原请求' : '开始生成' }}</UButton>
        <NuxtLink v-if="operation?.jobId" :to="`/jobs/${operation.jobId}`"><UButton>查看上次任务</UButton></NuxtLink>
        <UButton v-if="operation && ['complete', 'failed'].includes(operation.phase)" :loading="busy" @click="generate(true)">重新生成</UButton>
      </section>
    </div>
  </div>
</template>
