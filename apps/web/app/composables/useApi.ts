import { createApiClient } from '~/utils/api-client'

export const useApi = () => {
  const config = useRuntimeConfig()
  const { accessToken } = useAuth()
  return createApiClient(config.public.apiBase, accessToken)
}
