import { authClient } from '~/lib/auth-client'

export const useAuth = () => {
  const session = useState<Awaited<ReturnType<typeof authClient.getSession>>['data'] | null>(
    'auth-session',
    () => null
  )

  const refresh = async () => {
    const result = await authClient.getSession()
    session.value = result.data
    return result.data
  }

  const accessToken = async () => {
    const result = await authClient.token()
    if (result.error || !result.data?.token) throw new Error('无法获取 API 访问令牌')
    return result.data.token
  }

  const logout = async () => {
    await authClient.signOut()
    session.value = null
    await navigateTo('/login')
  }

  return { session, refresh, accessToken, logout }
}
