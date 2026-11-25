# AzomOps launcher for Windows PowerShell
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Skills = Join-Path $Root "skills"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$Skills;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $Skills
}
if (-not $env:AZOM_CONFIG_DIR) {
    $env:AZOM_CONFIG_DIR = Join-Path $Root "config"
}
if (-not $env:AZOM_DATA_DIR) {
    $env:AZOM_DATA_DIR = Join-Path $Root ".azom-data"
}

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
    $Py = $VenvPy
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Py = "python"
} else {
    Write-Error "Python 3.11+ hittades inte."
    exit 1
}

Push-Location $Root
try {
    & $Py -m ecom_ops.launcher @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
