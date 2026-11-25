[CmdletBinding()]
param(
    [string] $Version = $(if ($env:WINDOWS_EXPORTER_VERSION) { $env:WINDOWS_EXPORTER_VERSION } else { '0.31.7' })
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$toolDirectory = Join-Path $repoRoot '.tools\windows_exporter'
$executable = Join-Path $toolDirectory 'windows_exporter.exe'

if (Test-Path -LiteralPath $executable) {
    Write-Output $executable
    return
}

New-Item -ItemType Directory -Path $toolDirectory -Force | Out-Null
$downloadUrl = "https://github.com/prometheus-community/windows_exporter/releases/download/v$Version/windows_exporter-$Version-amd64.exe"
$temporaryFile = "$executable.download"

Write-Host "Downloading windows_exporter $Version from the official GitHub release..."
Invoke-WebRequest -Uri $downloadUrl -OutFile $temporaryFile -UseBasicParsing
Move-Item -LiteralPath $temporaryFile -Destination $executable -Force
Write-Output $executable

