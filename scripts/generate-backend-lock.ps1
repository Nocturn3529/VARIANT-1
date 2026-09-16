# Generate the complete closure from the configured backend environment.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendPython = Join-Path $projectRoot "backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $backendPython)) {
    throw "Backend venv not found. Run npm run setup:backend first."
}
& $backendPython (Join-Path $PSScriptRoot "generate_backend_lock.py") @args
if ($LASTEXITCODE -ne 0) { throw "Dependency validation failed; existing lock was preserved." }
