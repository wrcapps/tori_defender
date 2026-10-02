# One-time setup on Windows: Python environment, NAS folder, and a starter config.
# Run in PowerShell from this folder:   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# NAS CREDENTIALS ARE NEVER WRITTEN TO A FILE HERE. If you pick a mapped drive, Windows itself
# remembers the login (net use /persistent:yes); a \\server\share path needs no mapping if you are
# already signed in to it in Explorer.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "=== bird-review-app Windows setup ==="

# ---- 1. Python environment ---------------------------------------------------------------
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
if (-not $py) { throw "Python 3.10+ not found. Install it from python.org (tick 'Add to PATH'), then run this again." }
if (-not (Test-Path .venv)) { & $py.Source -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

# GPU: the default PyTorch wheel on Windows is CPU-only. Offer the CUDA build if an NVIDIA GPU is there.
$gpu = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($gpu) {
    $a = Read-Host "NVIDIA GPU found. Install the CUDA build of PyTorch so detection can use it? [Y/n]"
    if ($a -ne "n" -and $a -ne "N") {
        & .\.venv\Scripts\python.exe -m pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cu124
    }
    # Camera video decoding on the GPU (NVDEC). Optional: without it video is decoded on the CPU.
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        & .\.venv\Scripts\python.exe -m pip install -q --no-deps -r requirements-gpu.txt
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not install PyNvVideoCodec; video will be decoded on the CPU (everything still works)." }
    } else {
        Write-Host "ffmpeg is not installed (winget install ffmpeg), so GPU video decoding is skipped."
        Write-Host "Install it, then run: .\.venv\Scripts\python.exe -m pip install --no-deps -r requirements-gpu.txt"
    }
} else {
    Write-Host "No NVIDIA GPU detected: everything runs on the CPU. Nothing to choose - the app uses the GPU by itself when there is one."
}

# ---- 2. NAS ---------------------------------------------------------------------------------
$nas = Read-Host "NAS folder with the site data, e.g. \\192.168.1.138\Pasari\Dataset (Enter to skip, set it later in Settings)"
if ($nas) {
    if ($nas -match '^\\\\[^\\]+\\[^\\]+') {
        $share = ($nas -split '\\')[0..3] -join '\'
        if (-not (Test-Path $nas)) {
            Write-Host "Not reachable yet. Enter the NAS login given to you (not your own):"
            $u = Read-Host "  username"
            $pw = Read-Host "  password" -AsSecureString
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
            net use $share /user:$u $plain /persistent:yes | Out-Null
            $plain = $null
        }
    }
    if (-not (Test-Path $nas)) { Write-Warning "$nas is still not reachable; you can fix the path later in Settings." }
    else { "data_dir: '$($nas.Replace("'", "''"))'`nsetup_done: false" | Set-Content -Encoding UTF8 settings.yaml }
}

# ---- 3. config + folders ------------------------------------------------------------------------
if (-not (Test-Path config.yaml)) {
    $site = Read-Host "Site name (short, lowercase, e.g. corbu or babadag)"
    $camHosts = ""; $camUser = "admin"; $env:BIRD_CAM_PASSWORD = ""
    $add = Read-Host "Connect cameras now? [y/N]"
    if ($add -match '^[Yy]') {
        Write-Host "The cameras' own login (the one you use in their web page), not this app's login."
        $u = Read-Host "  camera username [admin]"; if ($u) { $camUser = $u }
        $pw = Read-Host "  camera password" -AsSecureString
        $env:BIRD_CAM_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
        Write-Host "  Camera IP addresses, separated by commas or spaces (add =name to name one, e.g. 192.168.88.41=mast1-a)."
        $camHosts = Read-Host "  cameras"
    }
    # The writer escapes everything; the password travels in the environment, not on the command line.
    & .\.venv\Scripts\python.exe backend\make_config.py --site $site --user $camUser --hosts $camHosts --out config.yaml
    if ($LASTEXITCODE -ne 0) { throw "Could not write config.yaml (see the message above); run install.ps1 again." }
    Remove-Item Env:BIRD_CAM_PASSWORD -ErrorAction SilentlyContinue
    if (-not $camHosts) { Write-Host "No cameras added: Live will be empty. See 'Connecting your cameras' in README.md to add them later." }
}
New-Item -ItemType Directory -Force models, sites, logs | Out-Null

Write-Host ""
Write-Host "=== done ==="
Write-Host "1. Copy your model file(s) (.pt / .pth) into the 'models' folder."
Write-Host "2. Add a login:   .\.venv\Scripts\python.exe backend\auth.py --users users.yaml --add-user alice --role operator"
Write-Host "3. Start the app: double-click start.bat"
Write-Host "   On first start it walks you through the model and the data folder."
