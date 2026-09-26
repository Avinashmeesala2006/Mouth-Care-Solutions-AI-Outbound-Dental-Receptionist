[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$SourceAudio = '',
    [string]$OutputWav = ''
)

$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name was not found. $Hint"
    }
}

function Resolve-ProjectPath([string]$Path, [string]$Root) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    if ([IO.Path]::IsPathRooted($Path)) { return $Path }
    return Join-Path $Root $Path
}

function Get-AudioInfo([string]$Path) {
    $json = & ffprobe -v error -select_streams a:0 -show_entries stream=codec_name,sample_rate,channels -of json $Path 2>&1
    if ($LASTEXITCODE -ne 0) { return $null }
    try { return ($json | ConvertFrom-Json).streams | Select-Object -First 1 } catch { return $null }
}

Require-Command 'ffmpeg' 'Install FFmpeg and ensure ffmpeg.exe is on PATH.'
Require-Command 'ffprobe' 'Install FFmpeg and ensure ffprobe.exe is on PATH.'

$root = (Resolve-Path $RepoRoot).Path
$configuredOutput = if ($OutputWav) { $OutputWav } else { $env:FISH_SPEECH_REFERENCE_AUDIO }
if (-not $configuredOutput) { $configuredOutput = 'fish-references/mouth-care-receptionist-reference-20260919.wav' }
$outputPath = Resolve-ProjectPath $configuredOutput $root
$outputInfo = if (Test-Path $outputPath) { Get-AudioInfo $outputPath } else { $null }
if ($outputInfo -and $outputInfo.codec_name -eq 'pcm_s16le' -and [int]$outputInfo.sample_rate -eq 44100 -and [int]$outputInfo.channels -eq 1) {
    Write-Host "Using existing valid reference WAV: $outputPath"
    exit 0
}

$configuredSource = if ($SourceAudio) { $SourceAudio } else { $env:FISH_SPEECH_REFERENCE_SOURCE }
$sourcePath = Resolve-ProjectPath $configuredSource $root
if (-not $sourcePath) {
    $candidates = Get-ChildItem -Path $root -Recurse -File -Filter '*.mp4' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\(node_modules|\.venv|dist|build)\\' } |
        Sort-Object FullName
    $sourcePath = $candidates | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $sourcePath -or -not (Test-Path $sourcePath)) {
    throw "No MP4 voice recording was found. Pass -SourceAudio or set FISH_SPEECH_REFERENCE_SOURCE."
}

$sourceInfo = Get-AudioInfo $sourcePath
if (-not $sourceInfo) { throw "The source recording contains no readable audio stream: $sourcePath" }

New-Item -ItemType Directory -Force (Split-Path -Parent $outputPath) | Out-Null
& ffmpeg -hide_banner -loglevel error -y -i $sourcePath -vn -ar 44100 -ac 1 -c:a pcm_s16le $outputPath
if ($LASTEXITCODE -ne 0) { throw "FFmpeg failed while extracting audio from $sourcePath" }

$outputInfo = Get-AudioInfo $outputPath
if (-not $outputInfo -or $outputInfo.codec_name -ne 'pcm_s16le' -or [int]$outputInfo.sample_rate -ne 44100 -or [int]$outputInfo.channels -ne 1) {
    throw "Generated reference WAV failed validation: $outputPath"
}
Write-Host "Generated valid reference WAV: $outputPath"