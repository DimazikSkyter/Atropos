[CmdletBinding()]
param(
    [ValidateSet(3, 5)]
    [int] $Nodes = 3,
    [ValidateRange(2, 3600)]
    [int] $IntervalSeconds = 10,
    [switch] $Once,
    [switch] $DiskFull
)

. (Join-Path $PSScriptRoot 'common.ps1')

$insideCommand = "nodetool statusbinary; nodetool compactionstats | sed -n '1,12p'; echo 'SSTables / latency:'; nodetool tablestats compaction_lab.events 2>/dev/null | grep -E 'SSTable count|Local read latency|Local write latency|Space used \(live\)' || true; echo 'Data filesystem:'; df -h /var/lib/cassandra | tail -n 1"

do {
    Clear-Host
    Write-Host "Compaction lab - $(Get-Date -Format o)"
    foreach ($node in Get-LabNodes -Count $Nodes) {
        Write-Host ''
        Write-Host "===== $node ====="
        try {
            Invoke-LabCompose -Arguments @('exec', '-T', $node, 'bash', '-lc', $insideCommand) -DiskFull:$DiskFull
        }
        catch {
            Write-Host "unavailable: $($_.Exception.Message)" -ForegroundColor Red
        }
    }

    if (-not $Once) {
        Start-Sleep -Seconds $IntervalSeconds
    }
} while (-not $Once)
