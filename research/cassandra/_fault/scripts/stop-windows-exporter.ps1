[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $repoRoot '.tools\windows_exporter\windows_exporter.pid'

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host 'No lab-managed windows_exporter PID file found.'
    return
}

$exporterPid = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $exporterPid -ErrorAction SilentlyContinue
if ($process -and $process.ProcessName -like 'windows_exporter*') {
    Stop-Process -Id $exporterPid
    Write-Host "Stopped lab-managed windows_exporter (PID $exporterPid)."
}
Remove-Item -LiteralPath $pidFile -Force

