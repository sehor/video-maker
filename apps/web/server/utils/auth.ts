import { betterAuth } from 'better-auth'
import { jwt } from 'better-auth/plugins'
import pg from 'pg'

const pool = new pg.Pool({
  connectionString: process.env.BETTER_AUTH_DATABASE_URL
})

export const auth = betterAuth({
  database: pool,
  secret: process.env.BETTER_AUTH_SECRET,
  baseURL: process.env.BETTER_AUTH_URL || 'http://localhost:3000',
  trustedOrigins: [process.env.BETTER_AUTH_URL || 'http://localhost:3000'],
  emailAndPassword: { enabled: true },
  plugins: [
    jwt({
      jwt: {
        issuer: process.env.BETTER_AUTH_URL || 'http://localhost:3000',
        audience: process.env.AUTH_AUDIENCE || 'video-factory-api',
        expirationTime: '15m'
      }
    })
  ]
})
