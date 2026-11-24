<#
.SYNOPSIS
  Sets up the face helper for Better Organizing AI Add-on on Windows - the same
  as setup.sh on a Mac or Linux.

.DESCRIPTION
  A Python environment with OpenCV and onnxruntime, the face and picture
  models, and a shortcut in your Startup folder so it starts (without a window)
  when you sign in. It's started straight away too, and checked.

  The small models (YuNet, SFace, NudeNet) come with it, in models\ (see
  models\README.md for where each is from and its licence). The CLIP picture
  model (about 350 MB), CLIP's text model (about 65 MB) and its tokenizer are
  downloaded.

  Run it from this folder in PowerShell:
    powershell -ExecutionPolicy Bypass -File .\setup.ps1

.PARAMETER From
  Take every model, CLIP included, from this folder instead (a backup, e.g. on
  a NAS); nothing is downloaded.

.PARAMETER Python
  The Python to use, e.g. "C:\Python312\python.exe" or "py -3.12". By default
  the newest of 3.14 - 3.10 that the Python launcher (py) or PATH has.

.PARAMETER Dest
  Where to put it. Default: %LOCALAPPDATA%\face-helper.

.PARAMETER ListenAll
  Listen on the network (0.0.0.0), not just this computer: for Stash in Docker
  Desktop or on another computer. Windows Firewall may ask once.

.PARAMETER Port
  The port. Default 11535.

.PARAMETER Gpu
  Use an NVIDIA graphics card (installs onnxruntime-gpu; needs CUDA).

.PARAMETER NoStartup
  Don't start it at sign-in (no Startup shortcut); it's still started now.
#>
[CmdletBinding()]
param(
  [string]$From = "",
  [string]$Python = "",
  [string]$Dest = "",
  [switch]$ListenAll,
  [int]$Port = 11535,
  [switch]$Gpu,
  [switch]$NoStartup,
  [switch]$Body  # (kept for older instructions; NudeNet is always set up now)
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # (Invoke-WebRequest is many times slower with its progress bar)
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Dest) { $Dest = Join-Path $env:LOCALAPPDATA "face-helper" }
$Models = Join-Path $Dest "models"

$Small = @("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx", "nudenet_320n.onnx")
$Clip = "clip_vit_b32_vision.onnx"
$ClipUrl = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model.onnx"
$Extra = [ordered]@{  # CLIP's text half and tokenizer: search by description
  "clip_vit_b32_text_quantized.onnx" = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/text_model_quantized.onnx"
  "clip_tokenizer.json"              = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json"
}

function Say($text) { Write-Host $text }
# Windows PowerShell 5.1 turns anything a program writes to stderr (pip's
# warnings, "py -3.14" not being there) into an error, which "Stop" makes
# fatal: programs run with "Continue", and their exit code is what counts
function Run([scriptblock]$what) {
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & $what } finally { $ErrorActionPreference = $old }
}
function Fail($text) { Write-Host ""; Write-Host $text -ForegroundColor Red; exit 1 }

# --- a Python that onnxruntime has packages for (it lags behind the newest) -----
function Test-Python([string[]]$cmd) {
  try {
    $exe = $cmd[0]; $rest = @(); if ($cmd.Count -gt 1) { $rest = $cmd[1..($cmd.Count - 1)] }
    $v = Run { & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }
    if ($LASTEXITCODE -ne 0 -or -not $v) { return $null }
    $parts = "$v".Trim().Split(".")
    if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10 -and [int]$parts[1] -le 14) { return "$v".Trim() }
  } catch { }
  return $null
}
$Py = $null
if ($Python) {
  $Py = $Python.Split(" ")
  if (-not (Test-Python $Py)) { Fail "$Python isn't a Python 3.10 - 3.14 that runs." }
} else {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($m in 14, 13, 12, 11, 10) {
      if (Test-Python @("py", "-3.$m")) { $Py = @("py", "-3.$m"); break }
    }
  }
  if (-not $Py) {
    foreach ($name in "python", "python3") {
      $c = Get-Command $name -ErrorAction SilentlyContinue
      # (the Microsoft Store's "python" placeholder doesn't run: skipped)
      if ($c -and $c.Source -notlike "*WindowsApps*" -and (Test-Python @($c.Source))) { $Py = @($c.Source); break }
    }
  }
  if (-not $Py) {
    Fail ("No Python 3.10 - 3.14 found. Install one from https://www.python.org/downloads/windows/ " +
          "(tick 'Add python.exe to PATH'), or: winget install Python.Python.3.12 - then run this again.")
  }
}
$PyExe = $Py[0]; $PyArgs = @(); if ($Py.Count -gt 1) { $PyArgs = $Py[1..($Py.Count - 1)] }
Say ("Using Python " + (Test-Python $Py) + " (" + ($Py -join " ") + ")")

# --- the helper, its Python environment and packages -----------------------------
New-Item -ItemType Directory -Force -Path $Models | Out-Null

# a running helper has its files open: stop it first
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -and $_.CommandLine -like "*face_helper.py*" }
foreach ($p in $running) {
  Say "Stopping the face helper that's running (process $($p.ProcessId))"
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500

Copy-Item -Force (Join-Path $Here "face_helper.py") $Dest
$Venv = Join-Path $Dest "venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$VenvPyw = Join-Path $Venv "Scripts\pythonw.exe"
if (-not (Test-Path $VenvPy)) {
  Say "Making the Python environment in $Venv"
  Run { & $PyExe @PyArgs -m venv $Venv }
  if ($LASTEXITCODE -ne 0) { Fail "Couldn't make the Python environment (python -m venv)." }
}
$ort = "onnxruntime"; if ($Gpu) { $ort = "onnxruntime-gpu" }
Say "Installing the Python packages (OpenCV, numpy, $ort, tokenizers) - a few minutes the first time"
Run { & $VenvPy -m pip install -q --disable-pip-version-check --upgrade pip 2>$null | Out-Null }
Run { & $VenvPy -m pip install -q --disable-pip-version-check opencv-python-headless numpy $ort tokenizers }
if ($LASTEXITCODE -ne 0) {
  Fail ("Couldn't install the Python packages. onnxruntime may not support this Python yet: " +
        "install Python 3.12 (winget install Python.Python.3.12) and run: .\setup.ps1 -Python 'py -3.12'")
}

# --- models -----------------------------------------------------------------------
$Sums = @{}
foreach ($line in Get-Content (Join-Path $Here "models\SHA256SUMS")) {
  $bits = $line.Trim() -split "\s+", 2
  if ($bits.Count -eq 2) { $Sums[$bits[1].TrimStart("*")] = $bits[0].ToLower() }
}
function Test-Model([string]$path, [string]$name) {
  if (-not (Test-Path $path) -or -not $Sums.ContainsKey($name)) { return $false }
  return (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower() -eq $Sums[$name]
}
function Get-File([string]$url, [string]$to) {
  $tmp = "$to.part"
  try {
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    Move-Item -Force $tmp $to
    return $true
  } catch {
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    return $false
  }
}

$Src = $From; if (-not $Src) { $Src = Join-Path $Here "models" }
foreach ($f in $Small) {
  $p = Join-Path $Src $f
  if (-not (Test-Path $p)) { Fail "Missing $p" }
  if (-not (Test-Model $p $f)) { Fail "$p isn't the expected file (its checksum doesn't match models\SHA256SUMS)." }
  Copy-Item -Force $p (Join-Path $Models $f)
}
$ClipPath = Join-Path $Models $Clip
if ($From) {
  if (-not (Test-Path (Join-Path $From $Clip))) { Fail "Missing $(Join-Path $From $Clip)" }
  Copy-Item -Force (Join-Path $From $Clip) $ClipPath
} elseif (-not (Test-Model $ClipPath $Clip)) {
  Say "Downloading the CLIP picture model (about 350 MB)"
  if (-not (Get-File $ClipUrl $ClipPath)) { Fail "Couldn't download $ClipUrl" }
}
if (-not (Test-Model $ClipPath $Clip)) { Say "Note: $Clip isn't the checked version (it still works if it's a CLIP ViT-B/32 vision model)." }
foreach ($name in $Extra.Keys) {
  $to = Join-Path $Models $name
  if ($From -and (Test-Path (Join-Path $From $name))) {
    Copy-Item -Force (Join-Path $From $name) $to
  } elseif (-not (Test-Path $to) -or (Get-Item $to).Length -eq 0) {
    Say "Downloading $name"
    if (-not (Get-File $Extra[$name] $to)) { Say "Note: couldn't download $name; search by description won't work." }
  }
}

# --- start it now, and at sign-in ---------------------------------------------------
$Listen = "127.0.0.1"; if ($ListenAll) { $Listen = "0.0.0.0" }
$HelperArgs = "`"$(Join-Path $Dest 'face_helper.py')`" --host $Listen --port $Port"
if (-not $NoStartup) {
  $Startup = [Environment]::GetFolderPath("Startup")
  $lnk = Join-Path $Startup "Face helper (Better Organizing AI).lnk"
  $sh = New-Object -ComObject WScript.Shell
  $s = $sh.CreateShortcut($lnk)
  $s.TargetPath = $VenvPyw  # (pythonw: no console window)
  $s.Arguments = $HelperArgs
  $s.WorkingDirectory = $Dest
  $s.Description = "Face helper for Better Organizing AI Add-on (Stash)"
  $s.Save()
  Say "It starts when you sign in (shortcut: $lnk)"
}
Start-Process -FilePath $VenvPyw -ArgumentList $HelperArgs -WorkingDirectory $Dest -WindowStyle Hidden

$health = $null
for ($i = 0; $i -lt 30 -and -not $health; $i++) {
  Start-Sleep -Seconds 1
  try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 } catch { }
}
Say ""
if ($health -and $health.ok) {
  $what = @()
  if ($health.embed) { $what += "picture fingerprints" }
  if ($health.body) { $what += "body detector" }
  if ($health.text) { $what += "search by description" }
  if ($health.gpu) { $what += "NVIDIA graphics card" }
  Write-Host "Face helper installed in $Dest and running on port $Port ($($what -join ', '))." -ForegroundColor Green
} else {
  Write-Host "Installed in $Dest, but it didn't answer on http://127.0.0.1:$Port/health yet." -ForegroundColor Yellow
  Say "Start it by hand to see why:  & `"$VenvPy`" `"$(Join-Path $Dest 'face_helper.py')`""
}
Say ""
Say "In Stash (Settings -> Plugins -> Better Organizing AI -> Setup: face helper address):"
Say "  Stash on this computer:          http://127.0.0.1:$Port  (the default)"
Say "  Stash in Docker Desktop:         http://host.docker.internal:$Port  (run this with -ListenAll)"
Say "  Stash on another computer:       http://<this computer's address>:$Port  (run this with -ListenAll)"
Say "The AI model for tags and titles runs in Ollama (https://ollama.com): ollama pull gemma3:4b"

# Stash installed directly on Windows runs plugins with "python3" first, and on
# most PCs that's the Microsoft Store's stand-in (the tasks fail with "exit
# status 9009"): say which Python to give Stash instead
$stub = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $stub -or $stub.Source -like "*\WindowsApps\*") {
  $real = (& $PyExe @PyArgs -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
  if ($real) {
    Say ""
    Write-Host "If Stash is installed directly on this PC (not in Docker): in Stash, Settings -> System ->" -ForegroundColor Yellow
    Write-Host "Python executable path, enter:  $real" -ForegroundColor Yellow
    Say "(Otherwise Windows hands Stash the Microsoft Store's stand-in for Python and the plugins' tasks fail.)"
  }
}
