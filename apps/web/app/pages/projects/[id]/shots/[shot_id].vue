<script setup lang="ts">
import type { Job, Quote, Shot, Wallet } from '~/types/domain'

const route = useRoute()
const api = useApi()
const shot = ref<Shot | null>(null)
const mode = ref('success')
const tier = ref<'FAST' | 'STUDIO'>('FAST')
const wallet = ref<Wallet | null>(null)
const busy = ref(false)
const error = ref('')
const modes = [
  { label: '成功', value: 'success' },
  { label: '延迟成功', value: 'delayed' },
  { label: 'Provider 失败', value: 'failure' },
  { label: '超时', value: 'timeout' },
  { label: '重复通知', value: 'duplicate' },
  { label: '损坏 MP4', value: 'corrupt' }
]
const tiers = [
  { label: '快速', value: 'FAST' },
  { label: '工作室', value: 'STUDIO' }
]

const unit = computed(() => `${tier.value}_MS`)
const availableMs = computed(() => wallet.value?.balances[unit.value]?.USER_AVAILABLE ?? 0)
const reservedMs = computed(() => wallet.value?.balances[unit.value]?.USER_RESERVED ?? 0)

const loadWallet = async () => { wallet.value = await api.request<Wallet>('/v1/wallet') }

const grantTestSeconds = async () => {
  busy.value = true
  error.value = ''
  try {
    wallet.value = await api.request<Wallet>('/v1/wallet/test-grants', {
      method: 'POST',
      body: JSON.stringify({
        tier: tier.value,
        amount_ms: 60_000,
        idempotency_key: `web-test-grant:${crypto.randomUUID()}`,
        reason: '本地开发测试秒数'
      })
    })
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}

const generate = async () => {
  busy.value = true
  error.value = ''
  try {
    const quote = await api.request<Quote>('/v1/quotes', {
      method: 'POST',
      body: JSON.stringify({
        shot_id: route.params.shot_id,
        tier: tier.value,
        resolution: '720p',
        variant_count: 1
      })
    })
    const job = await api.request<Job>('/v1/generations', {
      method: 'POST',
      body: JSON.stringify({
        shot_id: route.params.shot_id,
        quote_id: quote.id,
        mock_mode: mode.value
      })
    })
    await navigateTo(`/jobs/${job.id}`)
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}

onMounted(async () => {
  const [loadedShot] = await Promise.all([
    api.request<Shot>(`/v1/shots/${route.params.shot_id}`),
    loadWallet()
  ])
  shot.value = loadedShot
})
</script>

<template>
  <div v-if="shot">
    <div class="page-head">
      <div><p class="muted">镜头</p><h1>{{ shot.title }}</h1></div>
      <NuxtLink :to="`/projects/${route.params.id}`"><UButton color="neutral" variant="soft">返回项目</UButton></NuxtLink>
    </div>
    <div class="two-col">
      <section class="panel">
        <h2>镜头参数</h2>
        <p>{{ shot.prompt }}</p>
        <div class="actions muted"><span>{{ shot.duration_seconds }} 秒</span><span>{{ shot.aspect_ratio }}</span><span>Mock 路线</span></div>
      </section>
      <section class="panel form-stack">
        <div><h2>提交 Mock 任务</h2><p class="muted">先锁定报价与秒数，再执行可靠任务。</p></div>
        <UFormField label="质量档"><USelect v-model="tier" :items="tiers" class="w-full" /></UFormField>
        <div class="actions muted">
          <span>可用 {{ (availableMs / 1000).toFixed(1) }} 秒</span>
          <span>冻结 {{ (reservedMs / 1000).toFixed(1) }} 秒</span>
        </div>
        <UButton color="neutral" variant="soft" :loading="busy" @click="grantTestSeconds">领取 60 秒测试额度</UButton>
        <UFormField label="模拟情形"><USelect v-model="mode" :items="modes" class="w-full" /></UFormField>
        <UAlert v-if="error" color="error" :description="error" />
        <UButton :loading="busy" icon="i-lucide-play" @click="generate">开始生成</UButton>
      </section>
    </div>
  </div>
</template>
