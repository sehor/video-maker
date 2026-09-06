import pg from 'pg'

// Called with the root environment by scripts/dev.ps1; never prints the connection string.
const client = new pg.Client({
  connectionString: process.env.BETTER_AUTH_DATABASE_URL,
  connectionTimeoutMillis: 3000,
  options: '-c default_transaction_read_only=on -c statement_timeout=5000'
})
try {
  await client.connect()
  const { rows } = await client.query('select current_database() as database')
  const tables = await client.query(
    "select tablename from pg_tables where schemaname = 'public'"
  )
  const names = new Set(tables.rows.map(row => row.tablename))
  const missing = ['user', 'session', 'account', 'verification', 'jwks'].filter(t => !names.has(t))
  console.log(`Better Auth connected: ${rows[0].database}; missing tables: ${missing.join(', ') || 'none'}`)
  if (missing.length && !process.argv.includes('--allow-unmigrated')) {
    console.error('Run scripts/dev.ps1 migrate to apply Better Auth migrations.')
    process.exitCode = 1
  }
} catch (error) {
  console.error(`Better Auth connection/query failed (${error.code || error.name}); check URL, instance and database.`)
  process.exitCode = 1
} finally {
  await client.end()
}
