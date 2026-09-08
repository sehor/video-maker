export class ApiRequestError extends Error {
  constructor(message: string, public status: number, public code?: string, public requestId?: string,
    public resultUnknown = false) {
    super(message)
  }
}

export const isTemporaryError = (error: unknown) => !(error instanceof ApiRequestError)
  || error.resultUnknown || error.status === 0
  || error.status >= 500 || [408, 429].includes(error.status)
  || error.code === 'IDEMPOTENCY_IN_PROGRESS'
