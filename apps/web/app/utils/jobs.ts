import type { JobStatus } from '~/types/domain'

export const isTerminalStatus = (status: JobStatus) =>
  status === 'SUCCEEDED' || status === 'FAILED_FINAL' || status === 'CANCELLED'
  || status === 'EXPIRED' || status === 'REJECTED_POLICY'
