import { describe, expect, it } from 'vitest'
import { isTerminalStatus } from '../app/utils/jobs'

describe('isTerminalStatus', () => {
  it('recognizes terminal states', () => {
    expect(isTerminalStatus('SUCCEEDED')).toBe(true)
    expect(isTerminalStatus('FAILED_FINAL')).toBe(true)
    expect(isTerminalStatus('CANCELLED')).toBe(true)
    expect(isTerminalStatus('EXPIRED')).toBe(true)
    expect(isTerminalStatus('REJECTED_POLICY')).toBe(true)
    expect(isTerminalStatus('RUNNING')).toBe(false)
    expect(isTerminalStatus('CANCEL_REQUESTED')).toBe(false)
  })
})
