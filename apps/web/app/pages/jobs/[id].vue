<script setup lang="ts">
import type { Job } from '~/types/domain'

const route = useRoute()
const api = useApi()
const job = ref<Job | null>(null)
const videoUrl = ref('')
const error = ref('')
let timer: ReturnType<typeof setTimeout> | undefined

const load = async () => {
  job.value = await api.request<Job>(`/v1/generations/${route.params.id}`)
  const output = job.value.outputs?.[0]
  if (job.value.status === 'SUCCEEDED' && output && !videoUrl.value) {
    videoUrl.value = await api.download(`/v1/outputs/${output.id}/content`)
  }
  if (['CREATED', 'QUEUED', 'RUNNING'].includes(job.value.status)) timer = setTimeout(load, 800)
}
const cancel = async () => {
  try { job.value = await api.request<Job>(`/v1/generations/${route.params.id}/cancel`, { method: 'POST' }) }
  catch (e) { error.value = (e as Error).message }
}
onMounted(load)
onBeforeUnmount(() => { if (timer) clearTimeout(timer); if (videoUrl.value) URL.revokeObjectURL(videoUrl.value) })
</script>

<template>
  <div v-if="job">
    <div class="page-head">
      <div><p class="muted">生成任务</p><h1>{{ job.id.slice(0, 8) }}</h1></div>
      <div class="actions"><span class="status" :class="job.status">{{ job.status }}</span><UButton v-if="['CREATED','QUEUED','RUNNING'].includes(job.status)" color="error" variant="soft" @click="cancel">取消</UButton></div>
    </div>
    <UAlert v-if="error" color="error" :description="error" class="mb-4" />
    <div class="two-col">
      <section class="panel">
        <h2>结果</h2>
        <video v-if="videoUrl" :src="videoUrl" controls />
        <div v-else-if="job.status === 'FAILED_FINAL'">
          <p>{{ job.error_message }}</p><code>{{ job.error_code }}</code>
        </div>
        <p v-else-if="job.status === 'CANCELLED'" class="muted">任务已取消，不会产生输出。</p>
        <p v-else class="muted">正在等待 Mock Provider…</p>
      </section>
      <section class="panel">
        <h2>事件</h2>
        <div class="list">
          <div v-for="event in (job.events || [])" :key="event.id" class="row">
            <span>{{ event.event_type }}</span><span class="muted">{{ event.from_status || '—' }} → {{ event.to_status }}</span>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
