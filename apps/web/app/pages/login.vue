<script setup lang="ts">
import { authClient } from '~/lib/auth-client'

const mode = ref<'login' | 'register'>('login')
const name = ref('')
const email = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')

const submit = async () => {
  busy.value = true
  error.value = ''
  const result = mode.value === 'login'
    ? await authClient.signIn.email({ email: email.value, password: password.value })
    : await authClient.signUp.email({ name: name.value, email: email.value, password: password.value })
  busy.value = false
  if (result.error) {
    error.value = result.error.message || '操作失败'
    return
  }
  await navigateTo('/projects')
}
</script>

<template>
  <div class="login-page">
    <section class="login-copy">
      <span class="eyebrow">VIDEO SHOT FACTORY</span>
      <h1>把每个镜头，<br>变成可管理的生产任务。</h1>
      <p>阶段一本地环境 · Mock 视频生成 · 720p 路线准备</p>
    </section>
    <form class="panel login-card form-stack" @submit.prevent="submit">
      <div>
        <h2>{{ mode === 'login' ? '登录' : '创建账号' }}</h2>
        <p class="muted">进入项目与镜头工作区</p>
      </div>
      <UFormField v-if="mode === 'register'" label="名称">
        <UInput v-model="name" required class="w-full" />
      </UFormField>
      <UFormField label="邮箱">
        <UInput v-model="email" type="email" required class="w-full" />
      </UFormField>
      <UFormField label="密码">
        <UInput v-model="password" type="password" minlength="8" required class="w-full" />
      </UFormField>
      <UAlert v-if="error" color="error" :description="error" />
      <UButton type="submit" block :loading="busy">{{ mode === 'login' ? '登录' : '注册并进入' }}</UButton>
      <UButton color="neutral" variant="ghost" block @click="mode = mode === 'login' ? 'register' : 'login'">
        {{ mode === 'login' ? '没有账号？创建一个' : '已有账号？返回登录' }}
      </UButton>
    </form>
  </div>
</template>

<style scoped>
.login-page { min-height: 100vh; display: grid; grid-template-columns: 1.3fr .7fr; align-items: center; gap: 64px; width: min(1160px, calc(100% - 48px)); margin: auto; }
.login-copy h1 { font-size: clamp(42px, 6vw, 76px); line-height: 1.02; letter-spacing: -.055em; margin: 22px 0; }
.login-copy p { color: #94a3b8; font-size: 18px; }
.eyebrow { color: #60a5fa; letter-spacing: .18em; font-size: 12px; font-weight: 800; }
.login-card { padding: 30px; }
@media (max-width: 820px) { .login-page { grid-template-columns: 1fr; padding: 48px 0; gap: 24px; } .login-copy h1 { font-size: 44px; } }
</style>
