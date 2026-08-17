<script setup lang="ts">
import type { Job, Shot } from '~/types/domain'

const route = useRoute()
const api = useApi()
const shot = ref<Shot | null>(null)
const mode = ref('success')
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

const generate = async () => {
  busy.value = true
  error.value = ''
  try {
    const job = await api.request<Job>('/v1/generations', {
      method: 'POST', body: JSON.stringify({ shot_id: route.params.shot_id, mock_mode: mode.value })
    })
    await navigateTo(`/jobs/${job.id}`)
  } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}

onMounted(async () => { shot.value = await api.request<Shot>(`/v1/shots/${route.params.shot_id}`) })
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
        <div><h2>提交 Mock 任务</h2><p class="muted">选择情形以验证任务状态和错误处理。</p></div>
        <UFormField label="模拟情形"><USelect v-model="mode" :items="modes" class="w-full" /></UFormField>
        <UAlert v-if="error" color="error" :description="error" />
        <UButton :loading="busy" icon="i-lucide-play" @click="generate">开始生成</UButton>
      </section>
    </div>
  </div>
</template>
