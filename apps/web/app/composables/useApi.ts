type ApiError = { error?: { code?: string; message?: string; request_id?: string } }

export const useApi = () => {
  const config = useRuntimeConfig()
  const { accessToken } = useAuth()

  const request = async <T>(path: string, options: RequestInit = {}): Promise<T> => {
    const token = await accessToken()
    const headers = new Headers(options.headers)
    headers.set('Authorization', `Bearer ${token}`)
    if (options.method && options.method !== 'GET' && !headers.has('Idempotency-Key')) {
      headers.set('Idempotency-Key', crypto.randomUUID())
    }
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
    const response = await fetch(`${config.public.apiBase}${path}`, { ...options, headers })
    if (!response.ok) {
      const body = await response.json().catch(() => ({})) as ApiError
      throw new ApiRequestError(body.error?.message || `请求失败 (${response.status})`, response.status,
        body.error?.code, body.error?.request_id)
    }
    if (response.status === 204) return undefined as T
    return response.json() as Promise<T>
  }

  const download = async (path: string, signal?: AbortSignal) => {
    const token = await accessToken()
    const response = await fetch(`${config.public.apiBase}${path}`, {
      headers: { Authorization: `Bearer ${token}` }, signal
    })
    if (!response.ok) throw new ApiRequestError(`下载失败 (${response.status})`, response.status)
    const blob = await response.blob()
    signal?.throwIfAborted()
    return URL.createObjectURL(blob)
  }

  return { request, download }
}
import { ApiRequestError } from '~/utils/api-error'
