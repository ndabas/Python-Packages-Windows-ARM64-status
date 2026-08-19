<#
.SYNOPSIS
Test pip installation of a list of packages, each in a fresh Docker container,
and write the results to a CSV file.

.EXAMPLE
./scripts/Test-PackageInstalls.ps1 -ColumnPrefix py3.14 -Image python-arm64
#>
[CmdletBinding()]
param(
    # Column prefix for the CSV, e.g. 'py3.14' or 'py3.14_vs'
    [Parameter(Mandatory)]
    [string]$ColumnPrefix,

    [string]$Image = 'python-arm64',

    # Path to the docker CLI
    [string]$DockerExe = 'docker',

    [string]$ListPath = "$PSScriptRoot/../data/list.txt",

    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
if (-not $OutputPath) { $OutputPath = "results-$ColumnPrefix.csv" }

$packages = Get-Content $ListPath |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and -not $_.StartsWith('#') }

# Mounted into the container to receive pip's install report
$reportDir = Join-Path ([IO.Path]::GetTempPath()) "pip-reports-$PID"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$reportPath = Join-Path $reportDir 'report.json'

try {
    $rows = foreach ($pkg in $packages) {
        if ($env:GITHUB_ACTIONS) { Write-Host "::group::pip install $pkg" }
        else { Write-Host "===== pip install $pkg =====" }

        Remove-Item $reportPath -ErrorAction SilentlyContinue
        & $DockerExe run --rm -v "${reportDir}:C:\out" $Image `
            python -m pip install --report C:\out\report.json $pkg

        if ($env:GITHUB_ACTIONS) { Write-Host '::endgroup::' }

        $status = 'failure'
        $url = ''
        if ($LASTEXITCODE -eq 0 -and (Test-Path $reportPath)) {
            $status = 'success'
            $report = Get-Content $reportPath -Raw | ConvertFrom-Json
            $url = ($report.install |
                Where-Object { $_.requested } |
                ForEach-Object { $_.download_info.url }) -join ' '
        }
        [pscustomobject]@{
            package                          = $pkg
            "${ColumnPrefix}_install_status" = $status
            "${ColumnPrefix}_binary_url"     = $url
        }
    }
}
finally {
    Remove-Item $reportDir -Recurse -Force -ErrorAction SilentlyContinue
}

$rows | Export-Csv -Path $OutputPath -NoTypeInformation
Write-Host "Wrote $OutputPath"
