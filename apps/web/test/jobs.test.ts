import { describe, expect, it } from 'vitest'
import { isTerminalStatus } from '../app/utils/jobs'
import type { JobStatus } from '../app/types/domain'

describe('isTerminalStatus', () => {
  const expected = {
    CREATED: false, RESERVED: false, QUEUED: false, ROUTING: false,
    SUBMITTED: false, RUNNING: false, CANCEL_REQUESTED: false,
    POSTPROCESSING: false, VALIDATING: false,
    SUCCEEDED: true, FAILED_FINAL: true, CANCELLED: true, EXPIRED: true, REJECTED_POLICY: true
  } satisfies Record<JobStatus, boolean>

  it.each(Object.entries(expected))('%s terminal=%s', (status, terminal) => {
    expect(isTerminalStatus(status as JobStatus)).toBe(terminal)
  })
})
