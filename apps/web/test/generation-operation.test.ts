import { describe, expect, it, vi } from 'vitest'
import { GenerationOperationController } from '../app/utils/generation-operation'
import { ApiRequestError } from '../app/utils/api-error'

const storage = () => {
  const data = new Map<string, string>()
  return { getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value) }, removeItem: (key: string) => { data.delete(key) } }
}
describe('generation operation', () => {
  it('replays the accepted request after lost response, double click, and reload', async () => {
    const store = storage()
    const request = vi.fn().mockResolvedValueOnce({ id: 'quote' }).mockRejectedValueOnce(new TypeError('network'))
    const task = new GenerationOperationController('u', 's', store, request, () => 'operation')
    const first = task.run()
    expect(task.run()).toBe(first)
    await expect(first).rejects.toThrow('network')
    const original = request.mock.calls[1]
    const restored = new GenerationOperationController('u', 's', store, request)
    expect(() => restored.startNew()).toThrow('先恢复')
    request.mockResolvedValueOnce({ id: 'job' })
    expect(await restored.run()).toBe('job')
    expect(request.mock.calls[2]).toEqual(original)
    expect(await restored.run()).toBe('job')
    expect(request).toHaveBeenCalledTimes(3)
  })
  it('replays quote loss with its original key and does not replace an expired quote automatically', async () => {
    const store = storage()
    const request = vi.fn().mockRejectedValueOnce(new Error('lost quote'))
    const task = new GenerationOperationController('u', 's', store, request, () => 'op')
    await expect(task.run()).rejects.toThrow()
    request.mockResolvedValueOnce({ id: 'expired' }).mockRejectedValueOnce(new ApiRequestError('expired', 409, 'QUOTE_EXPIRED'))
    await expect(task.run()).rejects.toThrow('expired')
    expect(request.mock.calls[1]).toEqual(request.mock.calls[0])
    await expect(task.run()).rejects.toThrow('明确失败')
    expect(request).toHaveBeenCalledTimes(3)
    task.startNew()
    expect(task.read()).toBeNull()
  })
  it('isolates users and shots while keeping only recoverable metadata across logout', async () => {
    const store = storage()
    const request = vi.fn().mockRejectedValue(new ApiRequestError('login', 401))
    const task = new GenerationOperationController('a', 's', store, request, () => 'op')
    await expect(task.run()).rejects.toThrow()
    expect(new GenerationOperationController('b', 's', store, request).read()).toBeNull()
    expect(new GenerationOperationController('a', 'other', store, request).read()).toBeNull()
    expect(new GenerationOperationController('a', 's', store, request).read()?.id).toBe('op')
    expect(store.getItem(task.key)).not.toMatch(/token|Bearer|prompt/)
  })
  it('does not dispatch when durable operation storage fails', async () => {
    const request = vi.fn()
    const store = { ...storage(), setItem: () => { throw new Error('storage unavailable') } }
    await expect(new GenerationOperationController('a', 's', store, request).run()).rejects.toThrow()
    expect(request).not.toHaveBeenCalled()
  })
})
