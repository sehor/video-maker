export default defineNuxtRouteMiddleware(async to => {
  if (import.meta.server) return
  const { refresh } = useAuth()
  const session = await refresh()
  if (!session && to.path !== '/login') return navigateTo('/login')
  if (session && to.path === '/login') return navigateTo('/projects')
})
