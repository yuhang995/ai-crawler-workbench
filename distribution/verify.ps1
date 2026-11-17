$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw 'Run .\install.ps1 first.'
}
Set-Location -LiteralPath $ProjectRoot
& $PythonExe -c "import app, core.pipeline; from playwright.sync_api import sync_playwright; print('Imports OK')"
if ($LASTEXITCODE -ne 0) { throw 'Import verification failed.' }
& $PythonExe -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
Write-Host 'Installation verification passed.' -ForegroundColor Green
