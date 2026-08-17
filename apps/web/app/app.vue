<script setup lang="ts">
const route = useRoute()
const { logout } = useAuth()
</script>

<template>
  <UApp>
    <div class="app-shell">
      <header v-if="route.path !== '/login'" class="topbar">
        <NuxtLink to="/projects" class="brand">镜头工厂</NuxtLink>
        <nav>
          <NuxtLink to="/projects">项目</NuxtLink>
          <NuxtLink to="/jobs">任务</NuxtLink>
          <UButton color="neutral" variant="ghost" size="sm" @click="logout">退出</UButton>
        </nav>
      </header>
      <main :class="{ content: route.path !== '/login' }">
        <NuxtPage />
      </main>
    </div>
  </UApp>
</template>
