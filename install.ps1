param(
    [switch]$InstallCodex,
    [switch]$RunTests
)

$ErrorActionPreference = 'Stop'
$Script = Join-Path $PSScriptRoot 'distribution\install.ps1'
& $Script -InstallCodex:$InstallCodex -RunTests:$RunTests

