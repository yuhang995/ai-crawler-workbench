param(
    [int]$Port = 8773,
    [switch]$Lan
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw 'The environment is not installed. Run .\install.ps1 first.'
}
$ListenHost = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
Set-Location -LiteralPath $ProjectRoot
& $PythonExe app.py --host $ListenHost --port $Port
