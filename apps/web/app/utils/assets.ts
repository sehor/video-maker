import type { Asset } from '~/types/domain'
import type { ApiRequest } from './api-client'

export async function loadProjectAssets(request: ApiRequest, projectId: string) {
  const assets: Asset[] = []
  let cursor: string | null = null
  const seen = new Set<string>()
  do {
    const page: { items: Asset[]; next_cursor?: string | null } = await request(
      '/v1/projects/{project_id}/assets', { params: { project_id: projectId }, query: { cursor } })
    assets.push(...page.items)
    cursor = page.next_cursor ?? null
    if (cursor && seen.has(cursor)) throw new Error('素材列表加载失败，请重试')
    if (cursor) seen.add(cursor)
  } while (cursor)
  return assets
}
