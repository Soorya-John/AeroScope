$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host 'Creating project virtual environment...'
    python -m venv --system-site-packages .venv
}
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
Write-Host 'Starting AeroScope locally. Press Ctrl+C to stop.'
& $python start_dev.py
