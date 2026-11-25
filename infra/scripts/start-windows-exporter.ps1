[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$toolDirectory = Join-Path $repoRoot '.tools\windows_exporter'
$pidFile = Join-Path $toolDirectory 'windows_exporter.pid'
$logFile = Join-Path $toolDirectory 'windows_exporter.log'
$configFile = Join-Path $repoRoot 'infra\we\config.yml'

try {
    $response = Invoke-WebRequest -Uri 'http://127.0.0.1:9182/health' -UseBasicParsing -TimeoutSec 2
    if ($response.StatusCode -eq 200) {
        Write-Host 'windows_exporter is already reachable on port 9182.'
        return
    }
}
catch {
    # Expected when the exporter is not running yet.
}

$executable = & (Join-Path $PSScriptRoot 'install-windows-exporter.ps1')
$arguments = @(
    "--config.file=$configFile"
    '--web.listen-address=0.0.0.0:9182'
    "--log.file=$logFile"
)
$process = Start-Process -FilePath $executable `
    -ArgumentList $arguments `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        throw "windows_exporter exited with code $($process.ExitCode). See $logFile"
    }

    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:9182/health' -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Write-Host "windows_exporter started (PID $($process.Id), port 9182)."
            return
        }
    }
    catch {
        # Continue waiting until the exporter is ready.
    }
}

Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
throw "windows_exporter did not become ready on port 9182. See $logFile"
