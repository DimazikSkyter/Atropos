[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$toolDirectory = Join-Path $repoRoot '.tools\windows_exporter'
$pidFile = Join-Path $toolDirectory 'windows_exporter.pid'

try {
    $response = Invoke-WebRequest -Uri 'http://127.0.0.1:9182/metrics' -UseBasicParsing -TimeoutSec 2
    if ($response.StatusCode -eq 200) {
        Write-Host 'windows_exporter is already reachable on port 9182.'
        return
    }
}
catch {
    # Expected when the exporter is not running yet.
}

$executable = & (Join-Path $PSScriptRoot 'install-windows-exporter.ps1')
$process = Start-Process -FilePath $executable `
    -ArgumentList '--web.listen-address=0.0.0.0:9182' `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:9182/metrics' -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Write-Host "windows_exporter started (PID $($process.Id), port 9182)."
            return
        }
    }
    catch {
        # Continue waiting.
    }
}

throw 'windows_exporter did not become ready on port 9182.'

