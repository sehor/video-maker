import { isTemporaryError } from './api-error'

export type GenerationOperation = {
  version: 1
  id: string
  shotId: string
  phase: 'quote' | 'submit' | 'complete' | 'failed'
  quoteId?: string
  jobId?: string
}
type Request = <T>(path: string, options: RequestInit) => Promise<T>
type Store = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export class GenerationOperationController {
  readonly key: string
  private inFlight?: Promise<string>

  constructor(private userId: string, private shotId: string, private store: Store,
    private request: Request, private uuid = () => crypto.randomUUID()) {
    this.key = `generation-operation:v1:${userId}:${shotId}`
  }

  read(): GenerationOperation | null {
    const raw = this.store.getItem(this.key)
    if (!raw) return null
    const value = JSON.parse(raw) as GenerationOperation
    if (value.version !== 1 || value.shotId !== this.shotId || typeof value.id !== 'string'
      || !['quote', 'submit', 'complete', 'failed'].includes(value.phase)
      || (value.phase === 'submit' && typeof value.quoteId !== 'string')
      || (value.phase === 'complete' && typeof value.jobId !== 'string')) {
      throw new Error('本地操作记录无法恢复，请先在任务列表核对结果')
    }
    return value
  }

  startNew() {
    const previous = this.read()
    if (this.inFlight || (previous && !['complete', 'failed'].includes(previous.phase))) {
      throw new Error('请先恢复原请求并确认结果')
    }
    this.store.removeItem(this.key)
  }

  run(): Promise<string> {
    if (this.inFlight) return this.inFlight
    this.inFlight = this.resume().finally(() => { this.inFlight = undefined })
    return this.inFlight
  }

  private async resume() {
    const operation = this.read() ?? {
      version: 1 as const, id: this.uuid(), shotId: this.shotId, phase: 'quote' as const
    }
    if (operation.phase === 'complete') return operation.jobId!
    if (operation.phase === 'failed') throw new Error('原请求已明确失败，可选择重新生成')
    const save = () => this.store.setItem(this.key, JSON.stringify(operation))
    save() // Persist before any request; storage failure must not dispatch a new operation.
    try {
      if (operation.phase === 'quote') {
        const quote = await this.request<{ id: string }>('/v1/quotes', {
          method: 'POST', headers: { 'Idempotency-Key': `${operation.id}:quote` },
          body: JSON.stringify({ shot_id: this.shotId, tier: 'FAST', resolution: '720P', variant_count: 1 })
        })
        operation.quoteId = quote.id
        operation.phase = 'submit'
        save()
      }
      const job = await this.request<{ id: string }>('/v1/generations', {
        method: 'POST', headers: { 'Idempotency-Key': `${operation.id}:generate` },
        body: JSON.stringify({ shot_id: this.shotId, quote_id: operation.quoteId })
      })
      operation.jobId = job.id
      operation.phase = 'complete'
      save()
      return job.id
    } catch (error) {
      if (!isTemporaryError(error) && ![401, 403].includes((error as { status: number }).status)) {
        operation.phase = 'failed'
        save()
      }
      throw error
    }
  }
}
