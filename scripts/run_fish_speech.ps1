[CmdletBinding()]
param(
    [string]$FishSpeechDir = "$HOME\fish-speech",
    [ValidateSet("fish-speech-1.5", "s2-pro")]
    [string]$Model = "fish-speech-1.5",
    [string]$FishSpeechRef = "v1.5.1",
    [ValidateSet("auto", "cpu", "cuda")]
    [string]$Backend = "auto",
    [string]$ReferenceSource = "",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8080,
    [switch]$AllowCpu
)

$ErrorActionPreference = "Stop"
function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "$Name was not found. $Hint" }
}

Require-Command "python" "Install Python 3.12 and ensure it is on PATH."
Require-Command "ffmpeg" "Install FFmpeg and ensure ffmpeg.exe is on PATH."
Require-Command "git" "Install Git for Windows and ensure git.exe is on PATH."
$referenceScript = Join-Path $PSScriptRoot "prepare_reference_voice.ps1"
if (Test-Path $referenceScript) { & $referenceScript -SourceAudio $ReferenceSource }
$pythonVersion = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ([version]$pythonVersion -lt [version]"3.12") { throw "Fish Speech requires Python 3.12 or newer; found $pythonVersion." }

if (-not (Test-Path $FishSpeechDir)) {
    git clone --depth 1 --branch $FishSpeechRef https://github.com/fishaudio/fish-speech.git $FishSpeechDir
}
if (-not (Test-Path (Join-Path $FishSpeechDir "tools\api_server.py"))) { throw "$FishSpeechDir is not a Fish Speech checkout." }

$memoryGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
$hasNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($Backend -eq "cpu" -and $Model -eq "s2-pro" -and $memoryGb -lt 24 -and -not $AllowCpu) { throw "S2 Pro requires a substantially larger host; found $memoryGb GB. Use fish-speech-1.5 or explicitly pass -AllowCpu." }

$venv = Join-Path $FishSpeechDir ".venv"
$pythonExe = Join-Path $venv "Scripts\python.exe"
$hf = Join-Path $venv "Scripts\hf.exe"
if (-not (Test-Path $pythonExe)) {
    python -m venv $venv
    & $pythonExe -m pip install --upgrade pip
    if ($FishSpeechRef -like "v1.*") { & $pythonExe -m pip install -e "$FishSpeechDir[stable]" }
    else { & $pythonExe -m pip install -e "$FishSpeechDir[cu129]" }
}
if ($Backend -eq "auto") {
    $torchCuda = (& $pythonExe -c "import torch; print(torch.cuda.is_available())").Trim()
    $Backend = if ($hasNvidia -and $torchCuda -eq "True") { "cuda" } else { "cpu" }
}
if ($Backend -eq "cuda" -and -not $hasNvidia) { throw "CUDA requested but nvidia-smi is unavailable. Use -Backend cpu or a GPU host." }

$modelDir = Join-Path $FishSpeechDir "checkpoints\$Model"
if ($Model -eq "fish-speech-1.5") { $repo = "fishaudio/fish-speech-1.5" } else { $repo = "fishaudio/s2-pro" }
if (-not (Test-Path (Join-Path $modelDir "model.pth")) -and -not (Test-Path (Join-Path $modelDir "codec.pth"))) {
    New-Item -ItemType Directory -Force $modelDir | Out-Null
    & $hf download $repo --local-dir $modelDir
}

Push-Location $FishSpeechDir
try {
    if ($Model -eq "fish-speech-1.5") {
        $args = @("tools/api_server.py", "--listen", "$HostAddress`:$Port", "--device", $Backend, "--workers", "1", "--llama-checkpoint-path", "checkpoints/fish-speech-1.5", "--decoder-checkpoint-path", "checkpoints/fish-speech-1.5/firefly-gan-vq-fsq-8x1024-21hz-generator.pth")
    } else {
        $args = @("tools/api_server.py", "--listen", "$HostAddress`:$Port", "--device", $Backend, "--workers", "1")
        if ($Backend -eq "cuda") { $args += "--half" }
    }
    Write-Host "Starting Fish Speech $Model on $Backend at http://$HostAddress`:$Port"
    & $pythonExe @args
} finally { Pop-Location }
