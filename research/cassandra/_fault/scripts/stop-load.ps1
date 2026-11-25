[CmdletBinding()]
param([switch] $DiskFull)

. (Join-Path $PSScriptRoot 'common.ps1')
Invoke-LabCompose -Arguments @('stop', 'loader') -DiskFull:$DiskFull
Write-Host 'Workload stopped. Keep watch.ps1 running to observe whether compaction catches up.'

