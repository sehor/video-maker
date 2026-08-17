<script setup lang="ts">
import type { Asset, Project, Shot } from '~/types/domain'

const route = useRoute()
const api = useApi()
const project = ref<Project | null>(null)
const shots = ref<Shot[]>([])
const asset = ref<Asset | null>(null)
const error = ref('')
const form = reactive({ title: '', prompt: '', duration_seconds: 5, aspect_ratio: '16:9' as '16:9' | '9:16' })

const load = async () => {
  project.value = await api.request<Project>(`/v1/projects/${route.params.id}`)
  shots.value = (await api.request<{ items: Shot[] }>(`/v1/projects/${route.params.id}/shots`)).items
}
const createShot = async () => {
  const shot = await api.request<Shot>(`/v1/projects/${route.params.id}/shots`, { method: 'POST', body: JSON.stringify(form) })
  await navigateTo(`/projects/${route.params.id}/shots/${shot.id}`)
}
const upload = async (event: Event) => {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const body = new FormData()
  body.append('file', file)
  try {
    asset.value = await api.request<Asset>(`/v1/projects/${route.params.id}/assets`, { method: 'POST', body })
  } catch (e) { error.value = (e as Error).message }
}
onMounted(load)
</script>

<template>
  <div v-if="project">
    <div class="page-head"><div><p class="muted">项目</p><h1>{{ project.name }}</h1><p class="muted">{{ project.description }}</p></div></div>
    <div class="two-col">
      <section class="panel">
        <h2>新建镜头</h2>
        <form class="form-stack" @submit.prevent="createShot">
          <UFormField label="镜头名称"><UInput v-model="form.title" required class="w-full" /></UFormField>
          <UFormField label="提示词"><UTextarea v-model="form.prompt" :rows="5" required class="w-full" /></UFormField>
          <div class="two-col">
            <UFormField label="时长（秒）"><UInput v-model.number="form.duration_seconds" type="number" min="1" max="10" /></UFormField>
            <UFormField label="画幅"><USelect v-model="form.aspect_ratio" :items="['16:9', '9:16']" /></UFormField>
          </div>
          <UButton type="submit">创建镜头</UButton>
        </form>
      </section>
      <section class="form-stack">
        <div class="panel">
          <h2>参考素材</h2>
          <p class="muted">JPEG、PNG、WebP 或 MP4，最大 50 MB。</p>
          <input type="file" accept="image/jpeg,image/png,image/webp,video/mp4" @change="upload">
          <p v-if="asset" class="muted">已上传：{{ asset.original_filename }}</p>
          <UAlert v-if="error" color="error" :description="error" />
        </div>
        <div class="panel">
          <h2>现有镜头</h2>
          <div v-if="shots.length" class="list">
            <NuxtLink v-for="shot in shots" :key="shot.id" :to="`/projects/${project.id}/shots/${shot.id}`" class="row">
              <span>{{ shot.title }}</span><span class="muted">{{ shot.duration_seconds }}s · {{ shot.aspect_ratio }}</span>
            </NuxtLink>
          </div>
          <p v-else class="muted">暂无镜头</p>
        </div>
      </section>
    </div>
  </div>
</template>
