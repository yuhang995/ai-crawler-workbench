param(
    [switch]$InstallCodex,
    [switch]$RunTests
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

$PythonExe = $null
$PythonPrefix = @()
if ($env:CRAWLER_PYTHON -and (Test-Path -LiteralPath $env:CRAWLER_PYTHON)) {
    $PythonExe = $env:CRAWLER_PYTHON
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = 'py'
    $PythonPrefix = @('-3.10')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = 'python'
} else {
    throw 'Python 3.10+ was not found. Install Python, then rerun install.ps1.'
}

$Version = & $PythonExe @PythonPrefix -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) {
    throw 'Python could not be started.'
}
$Parts = $Version.Trim().Split('.')
if ([int]$Parts[0] -lt 3 -or ([int]$Parts[0] -eq 3 -and [int]$Parts[1] -lt 10)) {
    throw "Python 3.10+ is required. Found $Version."
}

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $PythonExe @PythonPrefix -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}

$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
& $VenvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
& $VenvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Playwright Chromium installation failed.' }

if ($InstallCodex -and -not (Get-Command codex -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw 'Codex installation requested, but npm was not found. Install Node.js LTS first.'
    }
    & npm install -g '@openai/codex'
    if ($LASTEXITCODE -ne 0) { throw 'Codex CLI installation failed.' }
}

& $VenvPython -c "import app; import core.pipeline; print('Python runtime OK')"
if ($LASTEXITCODE -ne 0) { throw 'Project import check failed.' }

if ($RunTests) {
    & $VenvPython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed.' }
}

Write-Host ''
Write-Host 'Installation completed.' -ForegroundColor Green
Write-Host 'Start the workbench with: .\start.ps1'
if (Get-Command codex -ErrorAction SilentlyContinue) {
    Write-Host 'Codex CLI found. Run codex login once if it is not authenticated.'
} else {
    Write-Host 'Codex CLI is optional for deterministic sites. Run install.ps1 -InstallCodex to add AI fallback.'
}
