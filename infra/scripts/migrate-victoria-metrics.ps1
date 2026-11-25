[CmdletBinding()]
param(
    [string]$ComposeFile = 'infra/docker-compose-monitoring.yml',
    [string]$SourceVolume
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Invoke-Docker {
    param([string[]]$Arguments)
    $output = & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker failed (exit $LASTEXITCODE): docker $($Arguments -join ' ')"
    }
    return $output
}

Push-Location $repoRoot
try {
    $compose = @('compose')
    if (Test-Path -LiteralPath 'infra/.env') {
        $compose += @('--env-file', 'infra/.env')
    }
    $compose += @('-f', $ComposeFile)
    $config = (Invoke-Docker -Arguments ($compose + @('config', '--format', 'json'))) -join "`n" | ConvertFrom-Json
    $mounts = @($config.services.'victoria-metrics'.volumes | Where-Object { $_.target -eq '/victoria-metrics-data' -and $_.type -eq 'bind' })
    if ($mounts.Count -ne 1) { throw 'Expected one VictoriaMetrics bind mount in Compose configuration.' }
    $destination = $mounts[0].source
    if (-not [IO.Path]::IsPathRooted($destination)) { throw 'VM_DATA_PATH must be an absolute host path.' }
    $marker = Join-Path $destination '.vm-migration-in-progress'
    if (Test-Path -LiteralPath $marker) {
        throw "Previous migration did not finish. Inspect $destination before retrying; the original volume is unchanged. Do not start VictoriaMetrics with a partial copy."
    }
    if (Test-Path -LiteralPath $destination) {
        if (-not (Test-Path -LiteralPath $destination -PathType Container)) { throw "Not a directory: $destination" }
        if (Get-ChildItem -LiteralPath $destination -Force | Select-Object -First 1) {
            Write-Host "VictoriaMetrics data already exists in $destination; migration skipped."
            return
        }
    }
    if (-not $SourceVolume) { $SourceVolume = "$($config.name)_victoria_metrics_data" }
    $volumes = @(Invoke-Docker -Arguments @('volume', 'ls', '--format', '{{.Name}}'))
    if ($volumes -notcontains $SourceVolume) {
        Write-Host "No legacy volume $SourceVolume; starting with an empty data directory."
        return
    }
    # Stop this project's old instance before reading its data. Other users must
    # release the volume too; never copy a database while it is being written.
    Invoke-Docker -Arguments ($compose + @('stop', 'victoria-metrics')) | Out-Host
    $users = @(Invoke-Docker -Arguments @('ps', '-q', '--filter', "volume=$SourceVolume"))
    if ($users.Count -gt 0) { throw "Volume $SourceVolume is still used by a running container. Stop it before migrating." }
    $sourceMount = "type=volume,source=$SourceVolume,target=/source,readonly"
    # The volume list check above prevents Docker from silently creating a missing source.
    $contents = (Invoke-Docker -Arguments @('run', '--rm', '--mount', $sourceMount, 'alpine:3.23', 'sh', '-c', 'find /source -mindepth 1 -maxdepth 1 | head -n 1')) -join ''
    if (-not $contents.Trim()) {
        Write-Host "Legacy volume $SourceVolume is empty; migration skipped."
        return
    }
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    # Recheck before writing, and leave a marker on failure to prevent a partial
    # copy from being mistaken for an already migrated database on the next run.
    if (Get-ChildItem -LiteralPath $destination -Force | Select-Object -First 1) {
        throw "Destination became non-empty: $destination"
    }
    Set-Content -LiteralPath $marker -Value $SourceVolume -Encoding ascii
    Invoke-Docker -Arguments @('run', '--rm', '--mount', $sourceMount, '--mount', "type=bind,source=$destination,target=/target", 'alpine:3.23', 'sh', '-c', 'cp -a /source/. /target/') | Out-Host
    Remove-Item -LiteralPath $marker
    Write-Host "VictoriaMetrics data copied from $SourceVolume to $destination. Original volume retained."
}
finally {
    Pop-Location
}
