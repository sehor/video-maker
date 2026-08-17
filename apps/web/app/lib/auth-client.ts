import { createAuthClient } from 'better-auth/vue'
import { jwtClient } from 'better-auth/client/plugins'

export const authClient = createAuthClient({
  baseURL: typeof window === 'undefined' ? undefined : window.location.origin,
  plugins: [jwtClient()]
})
