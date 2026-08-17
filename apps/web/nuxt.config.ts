export default defineNuxtConfig({
  compatibilityDate: '2026-08-17',
  devtools: { enabled: true },
  ssr: false,
  modules: ['@nuxt/ui', '@nuxt/eslint'],
  css: ['~/assets/css/main.css'],
  runtimeConfig: {
    public: {
      apiBase: process.env.NUXT_PUBLIC_API_BASE || 'http://localhost:8000',
      authBaseUrl: process.env.NUXT_PUBLIC_AUTH_BASE_URL || 'http://localhost:3000'
    }
  },
  typescript: { strict: true }
})
