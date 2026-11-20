param(
    [int]$Port = 8773,
    [switch]$Lan
)

$ErrorActionPreference = 'Stop'
$Script = Join-Path $PSScriptRoot 'distribution\start.ps1'
& $Script -Port $Port -Lan:$Lan

