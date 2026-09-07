import type { Asset } from '~/types/domain'

export async function loadProjectAssets(request: <T>(path: string) => Promise<T>, projectId: string) {
  const assets: Asset[] = []
  let cursor: string | null = null
  const seen = new Set<string>()
  do {
    const page: { items: Asset[]; next_cursor?: string | null } = await request(
      `/v1/projects/${projectId}/assets${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`)
    assets.push(...page.items)
    cursor = page.next_cursor ?? null
    if (cursor && seen.has(cursor)) throw new Error('素材列表加载失败，请重试')
    if (cursor) seen.add(cursor)
  } while (cursor)
  return assets
}
