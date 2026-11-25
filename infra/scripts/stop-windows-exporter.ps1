[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pidFile = Join-Path $repoRoot '.tools\windows_exporter\windows_exporter.pid'

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host 'No project-managed windows_exporter PID file found.'
    return
}

$exporterPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $exporterPid -ErrorAction SilentlyContinue
if ($process -and $process.ProcessName -like 'windows_exporter*') {
    Stop-Process -Id $exporterPid
    Write-Host "Stopped project-managed windows_exporter (PID $exporterPid)."
}
else {
    Write-Warning "PID $exporterPid does not belong to a running windows_exporter process."
}

Remove-Item -LiteralPath $pidFile -Force
