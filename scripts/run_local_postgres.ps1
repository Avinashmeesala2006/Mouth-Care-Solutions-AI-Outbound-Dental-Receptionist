[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'status',
    # Portable PostgreSQL (EDB binaries zip) with a cluster initialised in <PgRoot>\data
    [string]$PgRoot = 'E:\pgsql-portable'
)
# Local development PostgreSQL on 127.0.0.1:5432 (no Windows service, no admin rights needed).
# One-time setup: extract postgresql-17.x-windows-x64-binaries.zip to <PgRoot>\pgsql, then
#   <PgRoot>\pgsql\bin\initdb.exe -D <PgRoot>\data -U postgres --auth=scram-sha-256 --pwprompt -E UTF8 --locale=C
# and create the application role/database (see README "PostgreSQL").
$ErrorActionPreference = 'Stop'
$pgCtl = Join-Path $PgRoot 'pgsql\bin\pg_ctl.exe'
$data = Join-Path $PgRoot 'data'
$log = Join-Path $PgRoot 'postgres.log'
$pgIsReady = Join-Path $PgRoot 'pgsql\bin\pg_isready.exe'
if (-not (Test-Path $pgCtl)) { throw "pg_ctl.exe not found under $PgRoot\pgsql\bin" }
if (-not (Test-Path (Join-Path $data 'PG_VERSION'))) { throw "No initialised cluster at $data" }

function Test-Listening { [bool](Get-NetTCPConnection -State Listen -LocalPort 5432 -ErrorAction SilentlyContinue) }
# Listening is not enough: during startup/crash recovery the server listens but refuses connections.
function Test-Accepting { & $pgIsReady -h 127.0.0.1 -p 5432 -q 2>$null; $LASTEXITCODE -eq 0 }

switch ($Action) {
    'start' {
        $state = 'started'
        if (Test-Listening) {
            $state = 'running (reused)'
        } else {
            # Detached start so the server outlives this script.
            Start-Process -FilePath $pgCtl -ArgumentList @('-D', $data, '-l', $log, 'start') -WindowStyle Hidden | Out-Null
        }
        $deadline = (Get-Date).AddSeconds(120)   # crash recovery after an unclean shutdown can take a while
        while (-not (Test-Accepting) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
        if (-not (Test-Accepting)) { throw "PostgreSQL is not accepting connections; see $log" }
        Write-Output "POSTGRES=$state 127.0.0.1:5432 (accepting connections)"
    }
    'stop' {
        & $pgCtl -D $data stop -m fast
    }
    'status' {
        & $pgCtl -D $data status
    }
}
