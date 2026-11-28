# AzomOps launcher for Windows PowerShell
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    foreach ($rawLine in Get-Content -Path $EnvFile -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        if ($line.StartsWith("export ")) { $line = $line.Substring(7).Trim() }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        $val = $line.Substring($idx + 1).Trim()
        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            if ($val.Length -ge 2) { $val = $val.Substring(1, $val.Length - 2) }
        }
        $existing = [Environment]::GetEnvironmentVariable($key, "Process")
        if ([string]::IsNullOrWhiteSpace($existing)) {
            [Environment]::SetEnvironmentVariable($key, $val, "Process")
        }
    }
}

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
