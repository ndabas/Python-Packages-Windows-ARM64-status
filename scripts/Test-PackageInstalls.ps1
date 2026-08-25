<#
.SYNOPSIS
Test pip installation of a list of packages, each in a fresh Docker container,
and write the results to a CSV file.

.EXAMPLE
./scripts/Test-PackageInstalls.ps1 -ColumnPrefix py3.14 -Image python-arm64
#>
[CmdletBinding()]
param(
    # Column prefix for the CSV, e.g. 'py3.14' or 'py3.14_buildtools'
    [Parameter(Mandatory)]
    [string]$ColumnPrefix,

    [Parameter(Mandatory)]
    [string]$ListPath,

    [string]$Image = 'python-arm64',

    # Host directory mounted as pip's cache. Keyed on the column prefix so that
    # wheels built for one image variant are never reused by another.
    [string]$CacheDir,

    # Fail instead of falling back to a source build; for images without build tools
    [switch]$OnlyBinary,

    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
if (-not $OutputPath) { $OutputPath = "results-$ColumnPrefix.csv" }
if (-not $CacheDir) { $CacheDir = Join-Path ([IO.Path]::GetTempPath()) "pip-cache-$ColumnPrefix" }
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

$pipArgs = @('install', '--no-warn-script-location', '--report', 'C:\out\report.json')
if ($OnlyBinary) { $pipArgs += '--only-binary=:all:' }

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
        docker run --rm -v "${reportDir}:C:\out" -v "${CacheDir}:C:\pip-cache" $Image `
            python -m pip @pipArgs $pkg

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
    $global:LASTEXITCODE = 0
}

$rows | Export-Csv -Path $OutputPath -NoTypeInformation
Write-Host "Wrote $OutputPath"
