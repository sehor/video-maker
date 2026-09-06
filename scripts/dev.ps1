#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Task = 'check',
    [string]$EnvFile = '.env',
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$CommandArgs
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install the documented prerequisites first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'apps/api/.venv'))) {
    throw 'API dependencies are missing. Run: uv sync --project apps/api --extra dev'
}

Push-Location $repoRoot
try {
    # The helper uses python-dotenv, never PowerShell evaluation, and passes a child-only environment.
    & uv --cache-dir (Join-Path $repoRoot 'apps/api/.uv-cache') --no-python-downloads run `
        --project (Join-Path $repoRoot 'apps/api') --no-sync python `
        (Join-Path $PSScriptRoot 'dev.py') --env-file $EnvFile $Task @CommandArgs
    $commandExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $commandExitCode
