$ErrorActionPreference = 'Stop'

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:ComposeBase = @('-f', (Join-Path $script:RepoRoot 'compose.yaml'))

function Invoke-LabCompose {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Arguments,
        [switch] $DiskFull
    )

    $composeFiles = @($script:ComposeBase)
    if ($DiskFull) {
        $composeFiles += @('-f', (Join-Path $script:RepoRoot 'compose.diskfull.yaml'))
    }

    & docker compose @composeFiles @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

function Get-LabNodes {
    param([ValidateSet(3, 5)][int] $Count = 3)
    return 1..$Count | ForEach-Object { "cassandra$_" }
}

