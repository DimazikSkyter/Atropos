[CmdletBinding()]
param(
    [ValidateSet('cassandra1', 'cassandra2', 'cassandra3', 'cassandra4', 'cassandra5')]
    [string] $Node = 'cassandra1',
    [ValidateSet('Pause', 'Kill')]
    [string] $Mode = 'Kill',
    [switch] $DiskFull
)

. (Join-Path $PSScriptRoot 'common.ps1')

if ($Mode -eq 'Pause') {
    Invoke-LabCompose -Arguments @('pause', $Node) -DiskFull:$DiskFull
    Write-Host "$Node is paused. Resume it with: docker compose unpause $Node"
}
else {
    Invoke-LabCompose -Arguments @('kill', '-s', 'SIGKILL', $Node) -DiskFull:$DiskFull
    Write-Host "$Node was killed. The loader continues against the remaining RF=3 replicas."
}

