[CmdletBinding()]
param(
    [string]$Distro = 'Ubuntu',
    [switch]$SkipNetworkingConfig
)
# Reproducible Asterisk setup for the Mouth Care Solutions receptionist (Windows host + WSL2).
#  1. verifies WSL2 and the Linux distro (prints the exact install commands if missing)
#  2. enables WSL mirrored networking so Windows and Asterisk share 127.0.0.1 (AMI + FastAGI)
#  3. creates ASTERISK_AMI_SECRET in .env if missing (never printed) and sets CALL_PROVIDER=asterisk
#  4. renders the Asterisk configuration and installs/configures Asterisk inside WSL
# It never configures a phone line: add SIP trunk credentials or a GSM modem yourself.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$envFile = Join-Path $root '.env'

function Invoke-Wsl([string[]]$WslArgs) {
    $output = & wsl.exe @WslArgs 2>&1 | ForEach-Object { "$_" -replace "`0", '' }
    return @{ Code = $LASTEXITCODE; Text = ($output -join "`n").Trim() }
}

# 1. WSL + distro --------------------------------------------------------------------
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw 'wsl.exe not found: this Windows build does not support WSL.' }
$list = Invoke-Wsl @('--list', '--quiet')
if ($list.Code -ne 0 -or $list.Text -match 'not installed') {
    Write-Output 'WSL is not installed. Run this in an elevated PowerShell, reboot, then re-run this script:'
    Write-Output "    wsl --install -d $Distro"
    exit 2
}
$distros = $list.Text -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if ($distros -notcontains $Distro) {
    Write-Output "WSL distro '$Distro' is not installed (have: $($distros -join ', ')). Run:"
    Write-Output "    wsl --install -d $Distro"
    Write-Output 'then open it once to create the Linux user, and re-run this script.'
    exit 2
}

# 2. Mirrored networking (Windows 11 22H2+) -----------------------------------------------
if (-not $SkipNetworkingConfig) {
    $wslConfig = Join-Path $env:USERPROFILE '.wslconfig'
    $content = if (Test-Path $wslConfig) { Get-Content $wslConfig -Raw } else { '' }
    if ($content -notmatch '(?im)^\s*networkingMode\s*=\s*mirrored') {
        if (Test-Path $wslConfig) { Copy-Item $wslConfig "$wslConfig.bak-$(Get-Date -Format yyyyMMddHHmmss)" }
        if ($content -match '(?im)^\s*\[wsl2\]') {
            $content = $content -replace '(?im)^(\s*\[wsl2\]\s*)$', "`$1`r`nnetworkingMode=mirrored"
        } else {
            $content = $content.TrimEnd() + "`r`n[wsl2]`r`nnetworkingMode=mirrored`r`n"
        }
        Set-Content -Path $wslConfig -Value $content -Encoding ascii
        Write-Output "Enabled WSL mirrored networking in $wslConfig; restarting WSL to apply."
        & wsl.exe --shutdown | Out-Null
    }
}

# 3. .env: AMI secret + provider (values are never printed) ----------------------------------
$lines = if (Test-Path $envFile) { @(Get-Content $envFile) } else { @() }
function Set-EnvKey([string]$Key, [string]$Value, [switch]$OnlyIfMissing) {
    $existing = $script:lines | Where-Object { $_ -match "^\s*$Key\s*=\s*\S" }
    if ($existing -and $OnlyIfMissing) { return }
    $script:lines = @($script:lines | Where-Object { $_ -notmatch "^\s*$Key\s*=" }) + "$Key=$Value"
}
$bytes = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
Set-EnvKey 'ASTERISK_AMI_SECRET' (($bytes | ForEach-Object { $_.ToString('x2') }) -join '') -OnlyIfMissing
Set-EnvKey 'CALL_PROVIDER' 'asterisk'
Set-EnvKey 'ASTERISK_WSL_DISTRO' $Distro -OnlyIfMissing
Set-Content -Path $envFile -Value $lines -Encoding utf8
Write-Output 'Updated .env (ASTERISK_AMI_SECRET present, CALL_PROVIDER=asterisk).'

# 4. Render + install ------------------------------------------------------------------------
Push-Location $root
try { & $python scripts/asterisk/render_config.py; if ($LASTEXITCODE -ne 0) { throw 'config rendering failed' } } finally { Pop-Location }
$wslRoot = '/mnt/' + $root.Substring(0, 1).ToLower() + ($root.Substring(2) -replace '\\', '/')
$result = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--', 'bash', "$wslRoot/scripts/asterisk/install_asterisk.sh", "$wslRoot/telephony/asterisk/generated")
Write-Output $result.Text
if ($result.Code -ne 0 -or $result.Text -notmatch 'ASTERISK_INSTALL_OK') { throw 'Asterisk installation failed (see output above).' }
Write-Output 'Asterisk is installed and configured. Restart FastAPI (scripts/run_local_stack.ps1 -RestartApi), then check GET /api/telephony/preflight.'
