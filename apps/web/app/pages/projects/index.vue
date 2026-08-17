<script setup lang="ts">
import type { Project } from '~/types/domain'

const api = useApi()
const projects = ref<Project[]>([])
const open = ref(false)
const name = ref('')
const description = ref('')
const error = ref('')

const load = async () => {
  const data = await api.request<{ items: Project[] }>('/v1/projects')
  projects.value = data.items
}

const create = async () => {
  error.value = ''
  try {
    const project = await api.request<Project>('/v1/projects', {
      method: 'POST', body: JSON.stringify({ name: name.value, description: description.value || null })
    })
    open.value = false
    await navigateTo(`/projects/${project.id}`)
  } catch (e) { error.value = (e as Error).message }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-head">
      <div><h1>项目</h1><p class="muted">组织短剧与漫剧的镜头生产。</p></div>
      <UButton icon="i-lucide-plus" @click="open = true">新建项目</UButton>
    </div>
    <div v-if="projects.length" class="grid">
      <NuxtLink v-for="project in projects" :key="project.id" :to="`/projects/${project.id}`" class="panel card-link">
        <h3>{{ project.name }}</h3>
        <p class="muted">{{ project.description || '暂无说明' }}</p>
      </NuxtLink>
    </div>
    <div v-else class="empty">还没有项目，从第一个镜头计划开始。</div>
    <UModal v-model:open="open" title="新建项目">
      <template #body>
        <form class="form-stack" @submit.prevent="create">
          <UFormField label="项目名称"><UInput v-model="name" required class="w-full" /></UFormField>
          <UFormField label="说明"><UTextarea v-model="description" class="w-full" /></UFormField>
          <UAlert v-if="error" color="error" :description="error" />
          <UButton type="submit">创建项目</UButton>
        </form>
      </template>
    </UModal>
  </div>
</template>
