<script setup lang="ts">
import type { Job } from '~/types/domain'
import { DownloadResource, RecoveringPoller } from '~/utils/job-recovery'

const route = useRoute()
const api = useApi()
const job = ref<Job | null>(null)
const videoUrl = ref('')
const error = ref('')
const downloadError = ref('')
const downloadBusy = ref(false)
let disposed = false
const cancelController = new AbortController()
const activeStatuses = new Set<Job['status']>([
  'CREATED', 'RESERVED', 'QUEUED', 'ROUTING', 'SUBMITTED', 'RUNNING', 'CANCEL_REQUESTED',
])
const cancellableStatuses = new Set<Job['status']>([
  'CREATED', 'RESERVED', 'QUEUED', 'ROUTING', 'SUBMITTED', 'RUNNING',
])

const resource = new DownloadResource((url, message) => { videoUrl.value = url; downloadError.value = message })
const download = async () => {
  const output = job.value?.outputs?.find(item => item.id === job.value?.final_output_id)
  if (!output || disposed || downloadBusy.value) return
  downloadBusy.value = true
  await resource.load(signal => api.download(`/v1/outputs/${output.id}/content`, signal))
  if (!disposed) downloadBusy.value = false
}
const poller = new RecoveringPoller<Job>({
  fetch: signal => api.request<Job>(`/v1/generations/${route.params.id}`, { signal }),
  update: value => { job.value = value; if (value.status === 'SUCCEEDED' && !videoUrl.value) void download() },
  active: value => activeStatuses.has(value.status),
  error: message => { error.value = message }
})
const cancel = async () => {
  try {
    const value = await api.request<Job>(`/v1/generations/${route.params.id}/cancel`, { method: 'POST', signal: cancelController.signal })
    if (!disposed) { job.value = value; poller.start() }
  } catch (e) { if (!disposed) error.value = (e as Error).message }
}
onMounted(() => poller.start())
onBeforeUnmount(() => { disposed = true; poller.stop(); resource.dispose(); cancelController.abort() })
</script>

<template>
  <div v-if="error" class="mb-4">
    <UAlert color="error" :description="error" />
    <UButton @click="poller.start()">重试查询</UButton>
  </div>
  <div v-if="job">
    <div class="page-head">
      <div><p class="muted">生成任务</p><h1>{{ job.id.slice(0, 8) }}</h1></div>
      <div class="actions"><span class="status" :class="job.status">{{ job.status }}</span><UButton v-if="cancellableStatuses.has(job.status)" color="error" variant="soft" @click="cancel">取消</UButton></div>
    </div>
    <div class="two-col">
      <section class="panel">
        <h2>结果</h2>
        <video v-if="videoUrl" :src="videoUrl" controls />
        <div v-else-if="job.status === 'SUCCEEDED'">
          <p>{{ downloadError || '正在加载视频…' }}</p>
          <UButton v-if="downloadError" :loading="downloadBusy" @click="download">重试下载</UButton>
        </div>
        <div v-else-if="job.status === 'FAILED_FINAL'">
          <p>{{ job.error_message }}</p><code>{{ job.failure_code }}</code>
        </div>
        <p v-else-if="job.status === 'CANCEL_REQUESTED'" class="muted">已请求取消，正在等待任务终止。</p>
        <p v-else-if="job.status === 'CANCELLED'" class="muted">任务已取消，不会产生输出。</p>
        <p v-else class="muted">正在生成视频…</p>
      </section>
    </div>
  </div>
</template>
