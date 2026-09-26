[CmdletBinding()]
param(
    [string]$FishRoot = 'E:\fish-speech',
    [switch]$SkipFish,
    [switch]$RestartApi
)
# Starts (or reuses) Fish Speech :8080, FastAPI :8000 (which hosts the FastAGI server for
# Asterisk) and, when PUBLIC_BASE_URL is set, the ngrok tunnel for the web application; then
# prints the truthful live-call preflight. Asterisk itself is managed by scripts/asterisk_ctl.ps1.
# It never stops processes it did not start: a port held by something unhealthy is
# reported and the script stops. No real call is ever placed.
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
    $cfg = & $python -c "import json; from backend.app.core.config import settings; c=settings.resolve(); print(json.dumps({'origin': c.public_origin, 'errors': c.errors, 'mode': c.app_mode, 'mock': c.mock_mode, 'provider': c.call_provider}))" | ConvertFrom-Json
} finally { Pop-Location }
Write-Output "APP_MODE=$($cfg.mode) MOCK_MODE=$($cfg.mock) CALL_PROVIDER=$($cfg.provider) PUBLIC_ORIGIN=$($cfg.origin)"
foreach ($e in $cfg.errors) { Write-Warning "CONFIG_ERROR: $e" }

# 2. Fish Speech (used to generate the voice pack; calls play the verified pack).
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
        Write-Output "FISH_SPEECH=started pid=$($fish.Id)"
        Wait-Until { Test-Http 'http://127.0.0.1:8080/v1/health' } 300 'Fish Speech'
    }
}

# 3. FastAPI.
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
    $api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'backend.app.main:app', '--host', '127.0.0.1', '--port', '8000') `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir 'fastapi.out.log') -RedirectStandardError (Join-Path $logDir 'fastapi.err.log')
    Write-Output "FASTAPI=started pid=$($api.Id)"
    Wait-Until { Test-Http 'http://127.0.0.1:8000/health' } 60 'FastAPI'
}

# 4. Optional ngrok tunnel for the web application (telephony does not use it).
$tunnels = $null
try { $tunnels = (Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 3).tunnels } catch { }
if (-not $cfg.origin) {
    Write-Output 'NGROK=skipped (PUBLIC_BASE_URL not set; the public URL only serves the web application)'
} elseif ($null -ne $tunnels) {
    $match = @($tunnels | Where-Object { $_.public_url -eq $cfg.origin })
    if ($match.Count -eq 1 -and $match[0].config.addr -match ':8000$') {
        Write-Output "NGROK=running (reused) $($cfg.origin) -> $($match[0].config.addr)"
    } else {
        throw "An ngrok agent is already running with tunnel(s) $(@($tunnels.public_url) -join ', '); it is not serving $($cfg.origin) -> :8000. Not stopping it automatically."
    }
} else {
    $ngrok = Start-Process -FilePath 'ngrok' -ArgumentList @('http', '127.0.0.1:8000', '--url', $cfg.origin, '--log', 'stdout') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir 'ngrok.out.log') -RedirectStandardError (Join-Path $logDir 'ngrok.err.log')
    Write-Output "NGROK=started pid=$($ngrok.Id)"
    Wait-Until {
        try { @((Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 3).tunnels | Where-Object { $_.public_url -eq $cfg.origin }).Count -eq 1 } catch { $false }
    } 30 'ngrok tunnel'
}

# 5. Programmatic public-route verification and truthful preflight (no call is placed).
Push-Location $root
try { & $python scripts/verify_public_route.py; $verifyExit = $LASTEXITCODE } finally { Pop-Location }
Write-Output "VERIFY_EXIT=$verifyExit (0 only when LIVE_CALL_ALLOWED=true)"
