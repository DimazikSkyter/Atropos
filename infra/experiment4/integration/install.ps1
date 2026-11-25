param([string]$RepoRoot = 'C:\Users\dimaz\IdeaProjects\Atropos')
$ErrorActionPreference = 'Stop'
$stageRoot = Split-Path $PSScriptRoot -Parent
$infraRoot = Join-Path $RepoRoot 'infra'
if (!(Test-Path -LiteralPath (Join-Path $infraRoot 'docker-compose-monitoring.yml'))) {
    throw 'Existing Atropos monitoring compose is required.'
}
$targetRoot = Join-Path $infraRoot 'experiment4'
$backupRoot = Join-Path $stageRoot ('pre-install-backup-' + (Get-Date -Format yyyyMMdd-HHmmss))
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$scrapePath = Join-Path $infraRoot 'victoria-metrics\scrape.yml'
$makePath = Join-Path $RepoRoot 'Makefile'
Copy-Item -LiteralPath $scrapePath -Destination $backupRoot
Copy-Item -LiteralPath $makePath -Destination $backupRoot
New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
foreach ($name in @('app','load','integration')) {
    Copy-Item -LiteralPath (Join-Path $stageRoot $name) -Destination $targetRoot -Recurse -Force
}
foreach ($name in @('compose.yaml','run.py','verify.py','analyze_modes.py','gc_intervals.py','bucket_analysis.py','dashboard.json','README.md','.gitignore','render_report.py')) {
    Copy-Item -LiteralPath (Join-Path $stageRoot $name) -Destination (Join-Path $targetRoot $name) -Force
}
$scrape = [System.IO.File]::ReadAllText($scrapePath)
if ($scrape -notmatch '(?m)^\s*-\s*job_name:\s*experiment4\s*$') {
    $fragment = [System.IO.File]::ReadAllLines((Join-Path $stageRoot 'integration\scrape-fragment.yml'))
    $indented = ($fragment | Where-Object { $_ -notmatch '^#' } | ForEach-Object { '  ' + $_ }) -join "`n"
    [System.IO.File]::WriteAllText($scrapePath, $scrape.TrimEnd() + "`n`n" + $indented + "`n", [System.Text.UTF8Encoding]::new($false))
}
Copy-Item -LiteralPath (Join-Path $stageRoot 'dashboard.json') -Destination (Join-Path $infraRoot 'grafana\dashboards\experiment4-gc-distribution.json') -Force
$make = [System.IO.File]::ReadAllText($makePath)
if ($make -notmatch '(?m)^experiment4-run:') {
    $addition = @'

# Slow minor-GC trend and response-time distribution, reusing shared monitoring.
.PHONY: experiment4-up experiment4-run experiment4-down
experiment4-up:
	$(PYTHON) infra/experiment4/run.py up
experiment4-run:
	$(PYTHON) infra/experiment4/run.py run
experiment4-down:
	$(PYTHON) infra/experiment4/run.py down
'@
    [System.IO.File]::WriteAllText($makePath, $make.TrimEnd() + "`n" + $addition + "`n", [System.Text.UTF8Encoding]::new($false))
}
Invoke-WebRequest -Method Post -Uri 'http://localhost:8428/-/reload' -UseBasicParsing | Select-Object StatusCode
$taskGrafanaUser = if ($env:GRAFANA_ADMIN_USER) { $env:GRAFANA_ADMIN_USER } else { 'admin' }
$taskGrafanaPassword = if ($env:GRAFANA_ADMIN_PASSWORD) { $env:GRAFANA_ADMIN_PASSWORD } else { 'admin' }
$taskGrafanaAuth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($taskGrafanaUser + ':' + $taskGrafanaPassword))
Invoke-WebRequest -Method Post -Uri 'http://localhost:3001/api/admin/provisioning/dashboards/reload' -Headers @{Authorization=('Basic ' + $taskGrafanaAuth)} -UseBasicParsing | Select-Object StatusCode
Write-Output "Installed experiment4: $targetRoot; monitoring backup: $backupRoot"
