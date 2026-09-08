import { describe, expect, it, vi } from 'vitest'
import { createApiClient } from '../app/utils/api-client'
import { ApiRequestError, isTemporaryError } from '../app/utils/api-error'

describe('generated API contract transport', () => {
  it('keeps interrupted read responses retryable and accepts empty error envelopes', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{'))
      .mockResolvedValueOnce(Response.json(null, { status: 503 }))
    const api = createApiClient('', async () => 'token', fetcher)
    const error = await api.request('/v1/wallet').catch(error => error)
    expect(error.code).toBe('RESPONSE_INVALID')
    expect(error.resultUnknown).toBe(false)
    expect(isTemporaryError(error)).toBe(true)
    await expect(api.request('/v1/wallet')).rejects.toMatchObject({ status: 503 })
  })
  it('encodes parameters and query, serializes JSON, and preserves the operation key', async () => {
    const fetcher = vi.fn().mockImplementation(async () => Response.json({ id: 'project' }))
    const api = createApiClient('https://api.example', async () => 'token', fetcher)
    await api.request('/v1/projects', { method: 'POST', headers: { 'Idempotency-Key': 'original' }, body: { name: 'project' } })
    expect(fetcher.mock.calls[0][1].body).toBe('{"name":"project"}')
    expect(fetcher.mock.calls[0][1].headers.get('Idempotency-Key')).toBe('original')
    expect(fetcher.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer token')
    await api.request('/v1/projects/{project_id}/assets', { params: { project_id: 'a/b' }, query: { cursor: 'x&y', limit: 5 } })
    expect(fetcher.mock.calls[1][0]).toBe('https://api.example/v1/projects/a%2Fb/assets?cursor=x%26y&limit=5')
    expect(fetcher.mock.calls[1][1]).not.toHaveProperty('params')
  })
  it('preserves multipart boundaries and maps 204 to undefined', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ id: 'asset' })).mockResolvedValueOnce(new Response(null, { status: 204 }))
    const api = createApiClient('', async () => 'token', fetcher)
    const body = new FormData()
    body.append('file', new Blob(['png']), 'image.png')
    await api.request('/v1/projects/{project_id}/assets', { method: 'POST', params: { project_id: 'p' }, body })
    expect(fetcher.mock.calls[0][1].body).toBe(body)
    expect(fetcher.mock.calls[0][1].headers.has('Content-Type')).toBe(false)
    expect(await api.request('/v1/projects/{project_id}', { method: 'DELETE', params: { project_id: 'p' } })).toBeUndefined()
  })
  it('exposes API errors and marks lost mutations as unknown', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ error: { code: 'DENIED', message: 'denied', request_id: 'r' } }, { status: 403 })).mockRejectedValueOnce(new TypeError('network'))
    const api = createApiClient('', async () => 'token', fetcher)
    await expect(api.request('/v1/wallet')).rejects.toMatchObject({ status: 403, code: 'DENIED', requestId: 'r', resultUnknown: false })
    await expect(api.request('/v1/projects', { method: 'POST', body: { name: 'x' } })).rejects.toMatchObject({ status: 0, resultUnknown: true })
  })
  it('uses authenticated downloads and retains structured download errors', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('video')).mockResolvedValueOnce(Response.json({ error: { code: 'OUTPUT_INVALID', request_id: 'download' } }, { status: 422 }))
    const api = createApiClient('', async () => 'token', fetcher)
    const url = await api.download('/v1/outputs/{output_id}/content', { params: { output_id: 'o' } })
    expect(url).toMatch(/^blob:/)
    URL.revokeObjectURL(url)
    await expect(api.download('/v1/outputs/{output_id}/content', { params: { output_id: 'o' } })).rejects.toBeInstanceOf(ApiRequestError)
    expect(fetcher.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer token')
  })
})
