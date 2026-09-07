import { isTemporaryError } from './api-error'

export class RecoveringPoller<T> {
  private timer?: ReturnType<typeof setTimeout>
  private controller?: AbortController
  private stopped = true
  private failures = 0

  constructor(private readonly options: {
    fetch: (signal: AbortSignal) => Promise<T>
    update: (value: T) => void
    active: (value: T) => boolean
    error: (message: string) => void
  }) {}

  start() {
    this.stop()
    this.stopped = false
    this.failures = 0
    void this.poll()
  }

  stop() {
    this.stopped = true
    clearTimeout(this.timer)
    this.controller?.abort()
  }

  private async poll() {
    const controller = new AbortController()
    this.controller = controller
    try {
      const value = await this.options.fetch(controller.signal)
      if (this.stopped || controller.signal.aborted) return
      this.failures = 0
      this.options.error('')
      this.options.update(value)
      if (this.options.active(value)) this.timer = setTimeout(() => void this.poll(), 800)
    } catch (error) {
      if (this.stopped || controller.signal.aborted) return
      this.failures++
      const retry = isTemporaryError(error) && this.failures <= 5
      this.options.error(`${(error as Error).message}${retry ? '，正在重试…' : '，请重试查询。'}`)
      if (retry) this.timer = setTimeout(() => void this.poll(), Math.min(800 * 2 ** this.failures, 15000))
    }
  }
}

export class DownloadResource {
  private controller?: AbortController
  private disposed = false
  private url = ''

  constructor(private readonly changed: (url: string, error: string) => void) {}

  async load(download: (signal: AbortSignal) => Promise<string>) {
    if (this.disposed || this.controller) return
    const controller = new AbortController()
    this.controller = controller
    this.changed(this.url, '')
    try {
      const url = await download(controller.signal)
      if (this.disposed || controller.signal.aborted) { URL.revokeObjectURL(url); return }
      if (this.url) URL.revokeObjectURL(this.url)
      this.url = url
      this.changed(url, '')
    } catch (error) {
      if (!this.disposed && !controller.signal.aborted) this.changed(this.url, (error as Error).message)
    } finally { this.controller = undefined }
  }

  dispose() {
    this.disposed = true
    this.controller?.abort()
    if (this.url) URL.revokeObjectURL(this.url)
    this.url = ''
  }
}
