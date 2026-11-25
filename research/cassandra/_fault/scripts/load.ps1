[CmdletBinding()]
param(
    [string] $Duration = '30m',
    [ValidateRange(1, 512)]
    [int] $Threads = 48,
    [switch] $DiskFull
)

. (Join-Path $PSScriptRoot 'common.ps1')

$env:LOAD_DURATION = $Duration
$env:LOAD_THREADS = "$Threads"
Invoke-LabCompose -Arguments @('--profile', 'load', 'up', '-d', '--force-recreate', 'loader') -DiskFull:$DiskFull

Write-Host "Load started for $Duration with $Threads threads."
Write-Host 'Follow client latency: docker compose logs -f loader'

