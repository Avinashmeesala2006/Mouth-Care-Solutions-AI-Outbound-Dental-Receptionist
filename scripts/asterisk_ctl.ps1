[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('start', 'stop', 'restart', 'status')][string]$Action,
    [string]$Distro = 'Ubuntu'
)
# Start/stop/status for Asterisk running inside WSL. Status also queries the application's
# preflight so the real telephony readiness (interface, dialplan, AMI) is shown.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { Write-Output 'ASTERISK=unavailable (WSL not installed)'; exit 2 }
$service = "if [ -d /run/systemd/system ]; then systemctl $Action asterisk; else service asterisk $Action; fi"
if ($Action -ne 'status') {
    & wsl.exe -d $Distro -u root -- sh -c $service
    if ($LASTEXITCODE -ne 0) { throw "asterisk $Action failed" }
}
& wsl.exe -d $Distro -u root -- sh -c 'asterisk -rx "core show version" 2>/dev/null || echo ASTERISK=not_running'
try {
    $p = Invoke-RestMethod 'http://127.0.0.1:8000/api/telephony/preflight?refresh=1' -TimeoutSec 60
    foreach ($key in 'ASTERISK_RUNNING', 'ASTERISK_CONTROL_READY', 'ASTERISK_DIALPLAN_READY', 'TELEPHONY_INTERFACE_READY', 'SOFTWARE_READY_FOR_LIVE_CALL', 'LIVE_CALL_ALLOWED') {
        Write-Output "$key=$($p.$key)"
    }
    $p.BLOCKERS | ForEach-Object { Write-Output "BLOCKER: $_" }
} catch { Write-Output 'FASTAPI preflight unavailable (is FastAPI running on 127.0.0.1:8000?)' }
