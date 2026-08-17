type ApiError = { error?: { code?: string; message?: string; request_id?: string } }

export const useApi = () => {
  const config = useRuntimeConfig()
  const { accessToken } = useAuth()

  const request = async <T>(path: string, options: RequestInit = {}): Promise<T> => {
    const token = await accessToken()
    const headers = new Headers(options.headers)
    headers.set('Authorization', `Bearer ${token}`)
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
    const response = await fetch(`${config.public.apiBase}${path}`, { ...options, headers })
    if (!response.ok) {
      const body = await response.json().catch(() => ({})) as ApiError
      throw new Error(body.error?.message || `请求失败 (${response.status})`)
    }
    if (response.status === 204) return undefined as T
    return response.json() as Promise<T>
  }

  const download = async (path: string) => {
    const token = await accessToken()
    const response = await fetch(`${config.public.apiBase}${path}`, {
      headers: { Authorization: `Bearer ${token}` }
    })
    if (!response.ok) throw new Error('下载失败')
    return URL.createObjectURL(await response.blob())
  }

  return { request, download }
}
