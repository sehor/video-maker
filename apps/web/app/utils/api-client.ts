import type { paths } from '@video-factory/api-client'
import { ApiRequestError } from './api-error'

type Path = keyof paths
type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
type Methods<P extends Path> = {
  [M in Method]: Lowercase<M> extends keyof paths[P]
    ? NonNullable<paths[P][Lowercase<M>]> extends never ? never : M : never
}[Method]
type Operation<P extends Path, M extends Method> = NonNullable<paths[P][Lowercase<M> & keyof paths[P]]>
type GetPath = { [P in Path]: 'GET' extends Methods<P> ? P : never }[Path]
type Params<O> = O extends { parameters: { path: infer P } } ? { params: P } : { params?: never }
type Query<O> = O extends { parameters: { query?: infer Q } } ? { query?: Q } : { query?: never }
type BodyContent<B> = B extends { content: { 'application/json': infer J } } ? J
  : B extends { content: { 'multipart/form-data': unknown } } ? FormData : never
type Body<O> = O extends { requestBody: infer B } ? { body: BodyContent<B> } : { body?: never }
type Options<P extends Path, M extends Method> = Omit<RequestInit, 'method' | 'body'>
  & Params<Operation<P, M>> & Query<Operation<P, M>> & Body<Operation<P, M>>
type Args<O> = object extends O ? [options?: O] : [options: O]
type Success<R> = R[Extract<keyof R, 200 | 201 | 202 | 204>]
type Payload<R> = R extends { content: { 'application/json': infer J } } ? J : undefined
type Result<P extends Path, M extends Method> = Operation<P, M> extends { responses: infer R } ? Payload<Success<R>> : never

export interface ApiRequest {
  <P extends GetPath>(path: P, ...args: Args<Options<P, 'GET'>>): Promise<Result<P, 'GET'>>
  <P extends Path, M extends Exclude<Methods<P>, 'GET'>>(
    path: P, options: Options<NoInfer<P>, M> & { method: M }
  ): Promise<Result<P, M>>
}
type DownloadPath = Extract<GetPath, `${string}/content`>
type RuntimeOptions = Omit<RequestInit, 'body'> & { params?: object; query?: object; body?: unknown }

export function createApiClient(base: string, accessToken: () => Promise<string>, transport = fetch) {
  const url = (path: string, options: RuntimeOptions) => {
    let result = path
    for (const [name, value] of Object.entries(options.params ?? {})) {
      result = result.replace(`{${name}}`, encodeURIComponent(String(value)))
    }
    if (/\{[^}]+\}/.test(result)) throw new Error('缺少请求路径参数')
    const query = new URLSearchParams()
    for (const [name, value] of Object.entries(options.query ?? {})) {
      if (value !== undefined && value !== null) query.set(name, String(value))
    }
    return `${base}${result}${query.size ? `?${query}` : ''}`
  }
  const send = async (path: string, options: RuntimeOptions) => {
    const token = await accessToken()
    const headers = new Headers(options.headers)
    headers.set('Authorization', `Bearer ${token}`)
    const method = options.method ?? 'GET'
    if (method !== 'GET' && !headers.has('Idempotency-Key')) headers.set('Idempotency-Key', crypto.randomUUID())
    const { body, params: _params, query: _query, ...init } = options
    const multipart = body instanceof FormData
    if (body !== undefined && !multipart) headers.set('Content-Type', 'application/json')
    let response: Response
    try {
      response = await transport(url(path, options), {
        ...init, headers, body: body === undefined ? undefined : multipart ? body : JSON.stringify(body)
      })
    } catch (error) {
      if (options.signal?.aborted) throw error
      throw new ApiRequestError('网络中断，请恢复原请求', 0, undefined, undefined, method !== 'GET')
    }
    if (!response.ok) {
      const value = await response.json().catch(() => ({})) as { error?: { code?: string; message?: string; request_id?: string } }
      throw new ApiRequestError(value.error?.message || `请求失败 (${response.status})`, response.status,
        value.error?.code, value.error?.request_id ?? response.headers.get('x-request-id') ?? undefined,
        method !== 'GET' && (response.status >= 500 || response.status === 408))
    }
    return response
  }
  async function request<P extends GetPath>(path: P, ...args: Args<Options<P, 'GET'>>): Promise<Result<P, 'GET'>>
  async function request<P extends Path, M extends Exclude<Methods<P>, 'GET'>>(
    path: P, options: Options<NoInfer<P>, M> & { method: M }
  ): Promise<Result<P, M>>
  async function request(path: string, input?: unknown): Promise<unknown> {
    const options = (input ?? {}) as RuntimeOptions
    const response = await send(path, options)
    if (response.status === 204) return undefined
    try { return await response.json() } catch {
      throw new ApiRequestError('响应中断，请恢复原请求', response.status, undefined,
        response.headers.get('x-request-id') ?? undefined, options.method !== undefined && options.method !== 'GET')
    }
  }
  const download = async <P extends DownloadPath>(path: P, options: Options<P, 'GET'>) => {
    const response = await send(path, options)
    const blob = await response.blob()
    options.signal?.throwIfAborted()
    return URL.createObjectURL(blob)
  }
  return { request, download }
}
