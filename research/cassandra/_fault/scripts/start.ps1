[CmdletBinding()]
param(
    [ValidateSet(3, 5)]
    [int] $Nodes = 3,
    [switch] $DiskFull,
    [switch] $NoMonitoring,
    [switch] $NodeExporter
)

. (Join-Path $PSScriptRoot 'common.ps1')

if ($DiskFull -and $Nodes -ne 3) {
    throw 'The bounded disk-full scenario currently supports exactly 3 nodes.'
}

$env:EXPECTED_NODES = "$Nodes"
$services = @(Get-LabNodes -Count $Nodes) + 'init'
$arguments = @()
if ($Nodes -eq 5) {
    $arguments += @('--profile', 'five')
}
$arguments += @('up', '-d', '--wait', '--build')

if (-not $NoMonitoring) {
    try {
        & (Join-Path $PSScriptRoot 'start-windows-exporter.ps1')
    }
    catch {
        Write-Warning "Windows exporter was not started: $($_.Exception.Message)"
        Write-Warning 'Grafana will still start; the windows scrape target will be DOWN.'
    }
    $services += @('victoriametrics', 'grafana', 'cadvisor')
}

if ($NodeExporter) {
    $arguments = @('--profile', 'linux-host') + $arguments
    $services += 'node-exporter'
}

$arguments += $services

Invoke-LabCompose -Arguments $arguments -DiskFull:$DiskFull

Write-Host ''
Write-Host "Cassandra lab is ready ($Nodes nodes, RF=3)."
Write-Host 'Start workload:  .\scripts\load.ps1'
Write-Host 'Watch backlog:   .\scripts\watch.ps1'
Write-Host 'Fail first node: .\scripts\fail-node.ps1 -Node cassandra1 -Mode Kill'
if (-not $NoMonitoring) {
    $grafanaPort = if ($env:GRAFANA_PORT) { $env:GRAFANA_PORT } else { '33000' }
    Write-Host "Grafana:         http://localhost:$grafanaPort/d/cassandra-compaction-debt"
    Write-Host 'VictoriaMetrics: http://localhost:8428/vmui/'
    Write-Host 'cAdvisor:        http://localhost:38080/'
}
