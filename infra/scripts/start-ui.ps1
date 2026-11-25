param([int]$Port = 8501)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$uiRoot = Join-Path $projectRoot 'ui'
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Choose another -Port or use the running UI."
}
$logRoot = Join-Path $projectRoot '.tools\ui'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
# The monitoring Compose stack uses this local default. Explicit environment wins.
if (-not $env:INFLUXDB_TOKEN) { $env:INFLUXDB_TOKEN = 'atropos-local-replay-token' }
$env:ATROPOS_UI_DEMO = '0'
$env:PYTHONUTF8 = '1'
Remove-Item Env:SSLKEYLOGFILE -ErrorAction SilentlyContinue
& uv --directory $uiRoot sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Could not synchronize the UI environment.' }
& (Join-Path $uiRoot '.venv\Scripts\python.exe') -B -m atropos_ui.config_db migrate
if ($LASTEXITCODE -ne 0) { throw 'Could not migrate the configuration database.' }
$pythonExe = Join-Path $uiRoot '.venv\Scripts\python.exe'
$process = Start-Process -FilePath $pythonExe -ArgumentList @('-B', '-m', 'atropos_ui.serve', '--port', $Port) `
    -WorkingDirectory $uiRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logRoot 'stdout.log') -RedirectStandardError (Join-Path $logRoot 'stderr.log')
$process.Id | Set-Content -LiteralPath (Join-Path $logRoot 'process.pid')
Write-Output "Atropos: http://localhost:$Port (PID $($process.Id))"
