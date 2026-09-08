import type { ApiRequest } from '../app/utils/api-client'
import type { Project } from '../app/types/domain'

// Compiled by vue-tsc; never sent over the network.
export async function contractExamples(request: ApiRequest) {
  const project: Project = await request('/v1/projects', { method: 'POST', body: { name: 'typed' } })
  await request('/v1/projects/{project_id}', { params: { project_id: project.id } })
  await request('/v1/projects/{project_id}', { method: 'DELETE', params: { project_id: project.id } })
  // @ts-expect-error Unknown paths must not have a string fallback.
  await request('/v1/not-an-api')
  // @ts-expect-error Wallet has no POST operation.
  await request('/v1/wallet', { method: 'POST' })
  // @ts-expect-error A project needs a name.
  await request('/v1/projects', { method: 'POST', body: {} })
  // @ts-expect-error Request fields come from generated OpenAPI.
  await request('/v1/projects', { method: 'POST', body: { name: 'x', made_up: true } })
  // @ts-expect-error JSON must be typed before serialization.
  await request('/v1/projects', { method: 'POST', body: '{"name":"x"}' })
  // @ts-expect-error Template parameters are mandatory.
  await request('/v1/projects/{project_id}')
  // @ts-expect-error Parameter name is part of the generated contract.
  await request('/v1/projects/{project_id}', { params: { wrong_id: 'x' } })
  // @ts-expect-error Response is inferred, not asserted by the caller.
  const incorrect: number = await request('/v1/projects/{project_id}', { params: { project_id: 'x' } })
  return incorrect
}
