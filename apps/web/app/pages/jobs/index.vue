<script setup lang="ts">
import type { Job } from '~/types/domain'
const api = useApi()
const jobs = ref<Job[]>([])
onMounted(async () => { jobs.value = (await api.request<{ items: Job[] }>('/v1/jobs')).items })
</script>

<template>
  <div>
    <div class="page-head"><div><h1>任务</h1><p class="muted">查看所有 Mock 生成任务及结果。</p></div></div>
    <div v-if="jobs.length" class="list">
      <NuxtLink v-for="job in jobs" :key="job.id" :to="`/jobs/${job.id}`" class="panel row card-link">
        <div><strong>任务 {{ job.id.slice(0, 8) }}</strong><p class="muted">{{ job.mock_mode }} · {{ new Date(job.created_at).toLocaleString() }}</p></div>
        <span class="status" :class="job.status">{{ job.status }}</span>
      </NuxtLink>
    </div>
    <div v-else class="empty">还没有生成任务。</div>
  </div>
</template>
