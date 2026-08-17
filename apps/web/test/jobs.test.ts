import { describe, expect, it } from 'vitest'
import { isTerminalStatus } from '../app/utils/jobs'

describe('isTerminalStatus', () => {
  it('recognizes terminal states', () => {
    expect(isTerminalStatus('SUCCEEDED')).toBe(true)
    expect(isTerminalStatus('FAILED_FINAL')).toBe(true)
    expect(isTerminalStatus('CANCELLED')).toBe(true)
    expect(isTerminalStatus('RUNNING')).toBe(false)
  })
})
