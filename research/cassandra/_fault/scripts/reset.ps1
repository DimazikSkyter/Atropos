[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [switch] $DiskFull,
    [switch] $Force
)

. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $Force -and -not $PSCmdlet.ShouldProcess(
    'cassandra-compaction-lab containers and named data volumes',
    'Delete the complete lab state')) {
    return
}

Invoke-LabCompose -Arguments @('--profile', 'load', '--profile', 'five', '--profile', 'linux-host', 'down', '--volumes', '--remove-orphans') -DiskFull:$DiskFull
& (Join-Path $PSScriptRoot 'stop-windows-exporter.ps1')
Write-Host 'Lab containers, network, and Cassandra data volumes were removed.'
