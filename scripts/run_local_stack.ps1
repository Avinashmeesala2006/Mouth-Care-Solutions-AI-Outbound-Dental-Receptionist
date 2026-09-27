[CmdletBinding()]
param(
    [string]$FishRoot = 'E:\fish-speech',
    [string]$PgRoot = 'E:\pgsql-portable',
    [switch]$SkipFish,
    [switch]$SkipPostgres,
    [switch]$Ngrok,
    [switch]$RestartApi
)
# Local development stack on Windows:
#   PostgreSQL 127.0.0.1:5432 -> Fish Speech 1.5.1 127.0.0.1:8080 (private) -> migrations ->
#   FastAPI 127.0.0.1:8000 -> optional ngrok tunnel (PUBLIC_BASE_URL) for Twilio webhooks.
# It reuses healthy processes, never stops processes it did not start, and never places a call.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$fishPython = Join-Path $FishRoot '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'diagnostics\runtime'
New-Item -ItemType Directory -Force $logDir | Out-Null

function Test-Http([string]$Url) {
    try { return (Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch { return $false }
}
function Get-ListenerPids([int]$Port) {
    @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
}
function Wait-Until([scriptblock]$Condition, [int]$Seconds, [string]$What) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) { if (& $Condition) { return } ; Start-Sleep -Seconds 2 }
    throw "$What did not become ready within $Seconds seconds."
}

# 1. Effective configuration, resolved by the application itself (one source of truth).
Push-Location $root
try {
    $cfg = & $python -c "import json; from backend.app.core.config import settings as s; c=s.resolve(); print(json.dumps({'mode': c.app_mode, 'twilio': c.twilio_enabled, 'origin': c.public_origin, 'db': bool(s.database_url), 'errors': c.errors, 'warnings': c.warnings}))" | ConvertFrom-Json
} finally { Pop-Location }
Write-Output "APP_MODE=$($cfg.mode) TWILIO_ENABLED=$($cfg.twilio) DATABASE=$(if ($cfg.db) {'postgresql'} else {'memory'}) PUBLIC_ORIGIN=$($cfg.origin)"
foreach ($e in $cfg.errors) { Write-Warning "CONFIG_ERROR: $e" }
foreach ($w in $cfg.warnings) { Write-Output "CONFIG_WARNING: $w" }

# 2. PostgreSQL (local portable cluster).
if (-not $SkipPostgres -and $cfg.db -and (Test-Path (Join-Path $PgRoot 'pgsql\bin\pg_ctl.exe'))) {
    & (Join-Path $PSScriptRoot 'run_local_postgres.ps1') -Action start -PgRoot $PgRoot
    Push-Location $root
    try { & $python -m backend.app.db.migrations; if ($LASTEXITCODE -ne 0) { throw 'database migrations failed' } } finally { Pop-Location }
}

# 3. Fish Speech 1.5.1 (CPU on this host; private, loopback only).
if (-not $SkipFish) {
    if (Test-Http 'http://127.0.0.1:8080/v1/health') {
        Write-Output 'FISH_SPEECH=running (reused)'
    } else {
        $holders = Get-ListenerPids 8080
        if ($holders.Count -gt 0) { throw "Port 8080 is held by PID(s) $($holders -join ',') but Fish /v1/health is not OK; refusing to start a duplicate." }
        $fishArgs = @('tools/api_server.py', '--listen', '127.0.0.1:8080', '--device', 'cpu', '--workers', '1',
            '--llama-checkpoint-path', 'checkpoints/fish-speech-1.5',
            '--decoder-checkpoint-path', 'checkpoints/fish-speech-1.5/firefly-gan-vq-fsq-8x1024-21hz-generator.pth',
            '--decoder-config-name', 'firefly_gan_vq')
        $fish = Start-Process -FilePath $fishPython -ArgumentList $fishArgs -WorkingDirectory $FishRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir 'fish-server.out.log') -RedirectStandardError (Join-Path $logDir 'fish-server.err.log')
        Write-Output "FISH_SPEECH=started pid=$($fish.Id) (model load takes about 1-2 minutes on CPU)"
        Wait-Until { Test-Http 'http://127.0.0.1:8080/v1/health' } 300 'Fish Speech'
    }
}

# 4. FastAPI (Twilio webhooks and Fish voice assets).
$apiProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match 'uvicorn\s+backend\.app\.main:app' })
if ($RestartApi -and $apiProcesses.Count -gt 0) {
    $apiProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Output "FASTAPI=stopped pid(s) $($apiProcesses.ProcessId -join ',') for restart"
    Start-Sleep -Seconds 2
}
if (Test-Http 'http://127.0.0.1:8000/health') {
    Write-Output 'FASTAPI=running (reused)'
} else {
    $holders = Get-ListenerPids 8000
    if ($holders.Count -gt 0) { throw "Port 8000 is held by PID(s) $($holders -join ',') that is not a healthy FastAPI; refusing to start a duplicate." }
    $api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'backend.app.main:app', '--host', '127.0.0.1', '--port', '8000',
        '--proxy-headers', '--forwarded-allow-ips', '127.0.0.1') -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir 'fastapi.out.log') -RedirectStandardError (Join-Path $logDir 'fastapi.err.log')
    Write-Output "FASTAPI=started pid=$($api.Id)"
    Wait-Until { Test-Http 'http://127.0.0.1:8000/health' } 60 'FastAPI'
}

# 5. Optional ngrok tunnel: Twilio must reach https://<origin>/api/telephony/twilio/*.
if ($Ngrok) {
    if (-not $cfg.origin) { throw 'PUBLIC_BASE_URL is not set; configure your ngrok domain first.' }
    $tunnels = $null
    try { $tunnels = (Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 3).tunnels } catch { }
    if ($null -ne $tunnels) {
        $match = @($tunnels | Where-Object { $_.public_url -eq $cfg.origin })
        if ($match.Count -ne 1) { throw "An ngrok agent is running with $(@($tunnels.public_url) -join ', '); it does not serve $($cfg.origin). Not stopping it automatically." }
        Write-Output "NGROK=running (reused) $($cfg.origin)"
    } else {
        $ngrokProcess = Start-Process -FilePath 'ngrok' -ArgumentList @('http', '127.0.0.1:8000', '--url', $cfg.origin, '--log', 'stdout') -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir 'ngrok.out.log') -RedirectStandardError (Join-Path $logDir 'ngrok.err.log')
        Write-Output "NGROK=started pid=$($ngrokProcess.Id) $($cfg.origin)"
        Wait-Until { try { @((Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 3).tunnels | Where-Object { $_.public_url -eq $cfg.origin }).Count -eq 1 } catch { $false } } 30 'ngrok tunnel'
    }
}

# 6. Truthful readiness (no call is placed).
Push-Location $root
try { & $python scripts/verify_public_route.py; $verifyExit = $LASTEXITCODE } finally { Pop-Location }
Write-Output "VERIFY_EXIT=$verifyExit (0 only when LIVE_CALL_ALLOWED=true)"
