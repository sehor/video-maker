import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiRequestError } from '../app/utils/api-error'
import { DownloadResource, RecoveringPoller } from '../app/utils/job-recovery'

beforeEach(() => vi.useFakeTimers())
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

describe('job status recovery', () => {
  it('recovers from a transient failure and stops at terminal state', async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce('RUNNING').mockResolvedValue('SUCCEEDED')
    const update = vi.fn()
    const poller = new RecoveringPoller({ fetch, update, active: value => value === 'RUNNING', error: vi.fn() })
    poller.start()
    await vi.runAllTimersAsync()
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(update.mock.calls.map(call => call[0])).toEqual(['RUNNING', 'SUCCEEDED'])
    expect(vi.getTimerCount()).toBe(0)
    poller.stop()
  })

  it.each([401, 403, 404])('stops on stable HTTP %s errors', async (status) => {
    const fetch = vi.fn().mockRejectedValue(new ApiRequestError('denied', status))
    const poller = new RecoveringPoller({ fetch, update: vi.fn(), active: () => true, error: vi.fn() })
    poller.start()
    await vi.runAllTimersAsync()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('caps retries and allows explicit recovery', async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError('offline'))
    const poller = new RecoveringPoller({ fetch, update: vi.fn(), active: () => false, error: vi.fn() })
    poller.start()
    await vi.runAllTimersAsync()
    expect(fetch).toHaveBeenCalledTimes(6)
    fetch.mockResolvedValue('SUCCEEDED')
    poller.start()
    await vi.runAllTimersAsync()
    expect(fetch).toHaveBeenCalledTimes(7)
  })

  it('aborts pending requests and ignores late results after unmount', async () => {
    let resolve!: (value: string) => void
    let signal!: AbortSignal
    const update = vi.fn()
    const poller = new RecoveringPoller({
      fetch: (input) => { signal = input; return new Promise<string>(done => { resolve = done }) },
      update, active: () => true, error: vi.fn()
    })
    poller.start()
    poller.stop()
    expect(signal.aborted).toBe(true)
    resolve('RUNNING')
    await vi.runAllTimersAsync()
    expect(update).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('independent video download', () => {
  it('retries failures and revokes the displayed URL on disposal', async () => {
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const changed = vi.fn()
    const resource = new DownloadResource(changed)
    await resource.load(() => Promise.reject(new Error('download failed')))
    expect(changed).toHaveBeenLastCalledWith('', 'download failed')
    await resource.load(() => Promise.resolve('blob:video'))
    expect(changed).toHaveBeenLastCalledWith('blob:video', '')
    resource.dispose()
    expect(revoke).toHaveBeenCalledWith('blob:video')
  })

  it('aborts and revokes a URL returned after unmount', async () => {
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const changed = vi.fn()
    const resource = new DownloadResource(changed)
    let resolve!: (value: string) => void
    let signal!: AbortSignal
    const pending = resource.load(input => { signal = input; return new Promise(done => { resolve = done }) })
    resource.dispose()
    expect(signal.aborted).toBe(true)
    resolve('blob:late')
    await pending
    expect(revoke).toHaveBeenCalledWith('blob:late')
    expect(changed).toHaveBeenCalledTimes(1)
  })
})
