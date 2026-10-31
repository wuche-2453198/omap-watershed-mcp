<#
.SYNOPSIS
  OMAP Watershed MCP - Windows install / check script.

.DESCRIPTION
  - Creates or updates the conda-forge environment from environment.yml.
  - Verifies Python dependencies and that this repository imports.
  - Copies the MCP config example, creates a restricted secrets directory,
    and generates a local DSH Cordis patch with paths substituted.
  - Prints the DSH startup and acceptance steps.

  Security: this script never asks for or writes the Ovital pre-shared key (PSK).
  You must paste the PSK into the generated empty key file yourself, then
  restrict its ACL (see docs/install-windows.md).

  IMPORTANT: this script has NOT been executed on a real Windows machine in this
  project. Run it in a test environment first, then follow docs/install-windows.md.

.PARAMETER RepoRoot
  Repository root. Defaults to the parent of this script directory.

.PARAMETER EnvName
  conda environment name. Default: omap-mcp.

.PARAMETER CondaExe
  Full path to conda.exe. Auto-detected when omitted.

.PARAMETER PythonExe
  Use an existing Python interpreter instead of a conda environment
  (combine with -SkipEnv). Useful for an existing .venv.

.PARAMETER SecretsDir
  Directory for the PSK file. Keep it OUTSIDE the repository. Default: C:\work\omap-secrets.

.PARAMETER SkipEnv
  Do not create/update the conda environment; use -PythonExe or discover one.

.PARAMETER CheckOnly
  Validate files/dependencies and print the planned paths, but write nothing.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1 -CheckOnly -SkipEnv -PythonExe .venv\Scripts\python.exe
#>
[CmdletBinding()]
param(
    [string] $RepoRoot   = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)),
    [string] $EnvName    = 'omap-mcp',
    [string] $CondaExe   = '',
    [string] $PythonExe  = '',
    [string] $SecretsDir = 'C:\work\omap-secrets',
    [switch] $SkipEnv,
    [switch] $CheckOnly
)

$ErrorActionPreference = 'Stop'

function Write-Log([string]$Message) { Write-Host "[install] $Message" }
function Fail([string]$Message) {
    Write-Host "[install] ERROR: $Message" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 0. Basic checks
# ---------------------------------------------------------------------------
if ($PSVersionTable.PSVersion.Major -lt 5) { Fail "PowerShell 5.1 or newer is required." }
if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
    Fail "Repository root not found: $RepoRoot"
}
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
Write-Log "Repository root: $RepoRoot"

$requiredFiles = @(
    'environment.yml',
    'pyproject.toml',
    'ovital_mcp\dsh\ovital.cordis.yml',
    'ovital_mcp\dsh\config.example.yaml'
)
foreach ($relative in $requiredFiles) {
    $full = Join-Path $RepoRoot $relative
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { Fail "Missing file: $full" }
}

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    Write-Warning "This script targets Windows; current platform looks different."
}

# ---------------------------------------------------------------------------
# 1. Resolve Python environment
# ---------------------------------------------------------------------------
function Resolve-Conda {
    if ($CondaExe) {
        if (Test-Path -LiteralPath $CondaExe -PathType Leaf) { return $CondaExe }
        Fail "CondaExe not found: $CondaExe"
    }
    foreach ($name in @('conda', 'mamba', 'micromamba')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    $candidates = @(
        (Join-Path $env:USERPROFILE 'miniforge3\Scripts\conda.exe'),
        (Join-Path $env:USERPROFILE 'miniconda3\Scripts\conda.exe'),
        (Join-Path $env:USERPROFILE 'mambaforge\Scripts\conda.exe'),
        'C:\ProgramData\miniforge3\Scripts\conda.exe',
        'C:\ProgramData\miniconda3\Scripts\conda.exe'
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return ''
}

$python = ''
$conda = ''
if (-not $SkipEnv) {
    $conda = Resolve-Conda
    if (-not $conda) { Fail "conda/mamba not found. Install Miniforge or pass -PythonExe ... -SkipEnv." }
    Write-Log "conda: $conda"
    $envList = & $conda env list 2>&1 | Out-String
    $exists = $envList -match "(?m)^\s*$([regex]::Escape($EnvName))\s"
    if ($CheckOnly) {
        if ($exists) { Write-Log "check-only: would update environment $EnvName" }
        else { Write-Log "check-only: environment $EnvName does not exist; dependency check skipped" }
    } elseif ($exists) {
        Write-Log "Updating conda environment: $EnvName"
        & $conda env update -f (Join-Path $RepoRoot 'environment.yml') -n $EnvName --prune
        if ($LASTEXITCODE -ne 0) { Fail "conda env update failed with exit code $LASTEXITCODE" }
    } else {
        Write-Log "Creating conda environment: $EnvName (first solve can take a while)"
        & $conda env create -f (Join-Path $RepoRoot 'environment.yml') -n $EnvName
        if ($LASTEXITCODE -ne 0) { Fail "conda env create failed with exit code $LASTEXITCODE" }
    }
    if ($exists -or -not $CheckOnly) {
        $lines = @(& $conda run -n $EnvName python -c "import sys; print(sys.executable)" 2>&1)
        if ($LASTEXITCODE -ne 0) {
            if ($CheckOnly) { Write-Warning "could not resolve Python inside environment $EnvName" }
            else { Fail "could not resolve Python inside environment $EnvName" }
        } else {
            $python = ($lines | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 1).Trim()
        }
    }
} elseif ($PythonExe) {
    if (Test-Path -LiteralPath $PythonExe -PathType Leaf) {
        $python = (Resolve-Path -LiteralPath $PythonExe).Path
    } else {
        $command = Get-Command $PythonExe -ErrorAction SilentlyContinue
        if ($command) { $python = $command.Source } else { Fail "PythonExe not found: $PythonExe" }
    }
} else {
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $python = $venvPython
    } else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($command) { $python = $command.Source } else { Fail "No Python interpreter found; pass -PythonExe." }
    }
}

if ($python) { Write-Log "Python: $python" }

# ---------------------------------------------------------------------------
# 2. Dependency and importability check
# ---------------------------------------------------------------------------
if ($python) {
    Write-Log "Checking dependencies (mcp/rasterio/shapely/pyproj/numpy/scipy/matplotlib/pillow/websockets/yaml/pydantic)..."
    $checkScript = @'
import importlib
import sys

missing = []
for name in ("mcp", "rasterio", "shapely", "pyproj", "numpy", "scipy",
             "matplotlib", "PIL", "websockets", "yaml", "pydantic"):
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - report exactly what failed
        missing.append("%s: %s: %s" % (name, type(exc).__name__, exc))
if missing:
    sys.stderr.write("missing dependencies:\n  " + "\n  ".join(missing) + "\n")
    raise SystemExit(1)

import ovital_mcp
from ovital_mcp.config import load_config

print("deps ok; ovital_mcp", getattr(ovital_mcp, "__version__", "?"))
cfg = load_config()
print("config ok; ovital.configured =", cfg.safe_dict()["ovital"]["psk_configured"])
'@
    $env:PYTHONPATH = $RepoRoot
    try {
        & $python -c $checkScript
        if ($LASTEXITCODE -ne 0) { Fail "Dependency check failed; rebuild the environment from environment.yml." }
    } finally {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# 3. Config / secrets / local patch (skipped in -CheckOnly mode)
# ---------------------------------------------------------------------------
$configDir   = Join-Path $RepoRoot 'config'
$configFile  = Join-Path $configDir 'ovital.yaml'
$configSrc   = Join-Path $RepoRoot 'ovital_mcp\dsh\config.example.yaml'
$patchSrc    = Join-Path $RepoRoot 'ovital_mcp\dsh\ovital.cordis.yml'
$patchLocal  = Join-Path $RepoRoot 'ovital_mcp\dsh\ovital.cordis.local.yml'
$pskFile     = Join-Path $SecretsDir 'ovital_psk.txt'

Write-Log "Config file:  $configFile"
Write-Log "Secrets dir:  $SecretsDir"
Write-Log "Local patch:  $patchLocal"

if ($CheckOnly) {
    Write-Log "check-only: no files were written."
} else {
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null
    & icacls $SecretsDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "icacls on secrets dir failed; restrict the folder ACL manually." }

    $repoPosix = $RepoRoot.Replace('\', '/')
    $secretsPosix = (Resolve-Path -LiteralPath $SecretsDir).Path.Replace('\', '/')
    $pythonPosix = if ($python) { $python.Replace('\', '/') } else { 'C:/work/omap-mcp/.venv/Scripts/python.exe' }

    if (Test-Path -LiteralPath $configFile -PathType Leaf) {
        Write-Log "Config already exists, will not overwrite: $configFile"
    } else {
        Copy-Item -LiteralPath $configSrc -Destination $configFile
        $configText = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8
        $configText = $configText.Replace('C:/work/omap-mcp', $repoPosix)
        $configText = $configText.Replace('C:/work/omap-secrets', $secretsPosix)
        [IO.File]::WriteAllText($configFile, $configText, (New-Object System.Text.UTF8Encoding($false)))
        Write-Log "Created config (paths substituted): $configFile"
        Write-Log "  -> still review ovital.endpoint / gis.grass_cmd for this machine"
    }

    if (Test-Path -LiteralPath $pskFile -PathType Leaf) {
        Write-Log "PSK file already exists, will not overwrite: $pskFile"
    } else {
        [IO.File]::WriteAllText($pskFile, '', (New-Object System.Text.UTF8Encoding($false)))
        & icacls $pskFile /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
        Write-Log "Created empty PSK file: $pskFile"
        Write-Log "  -> Paste the Ovital PSK (single line, UTF-8 no BOM) into it, then keep the ACL restricted."
    }

    if (Test-Path -LiteralPath $patchLocal -PathType Leaf) {
        Write-Log "Local patch already exists, will not overwrite: $patchLocal"
    } else {
        $text = Get-Content -LiteralPath $patchSrc -Raw -Encoding UTF8
        $text = $text.Replace('C:/work/omap-mcp/.venv/Scripts/python.exe', $pythonPosix)
        $text = $text.Replace('C:/work/omap-mcp', $repoPosix)
        $text = $text.Replace('C:/work/omap-secrets', $secretsPosix)
        [IO.File]::WriteAllText($patchLocal, $text, (New-Object System.Text.UTF8Encoding($false)))
        Write-Log "Generated local patch (python/paths substituted): $patchLocal"
    }
}

# ---------------------------------------------------------------------------
# 4. Optional GRASS hint
# ---------------------------------------------------------------------------
if (-not $SkipEnv -and -not $CheckOnly -and $conda) {
    $envList = & $conda env list 2>&1 | Out-String
    if ($envList -match "(?m)^\s*omap-grass\s") {
        Write-Log "Found GRASS environment 'omap-grass'; set gis.grass_cmd to its launcher after verifying 'grass --version'."
    } else {
        Write-Log "GRASS is optional. To enable: conda create -n omap-grass -c conda-forge grass=8.5 -y"
    }
}

# ---------------------------------------------------------------------------
# 5. Next steps
# ---------------------------------------------------------------------------
@"

[install] Done. Next steps:
  1) Edit config: $configFile
     - ovital.endpoint must match the Ovital "third-party interface" setting (port, ws/wss)
     - ovital.psk_file must point to the restricted PSK file
     - Do NOT put the PSK in config/patch/env vars (DSH scrubs credential-like env names)
  2) Paste the PSK into: $pskFile  (single line, UTF-8 no BOM; keep ACL restricted)
  3) Start DSH:
       dsh web --patch "$patchLocal"
  4) In a DSH session call get_capabilities; expect 18 mcp__ovital__* tools.
  5) Acceptance steps: docs/install-windows.md section 6; pending items: docs/待核验清单.md

[install] NOTE: without a real Windows Ovital client, real-device interaction and
          boundary accuracy remain UNVERIFIED; mock/synthetic results do not count.
"@ | Write-Host
