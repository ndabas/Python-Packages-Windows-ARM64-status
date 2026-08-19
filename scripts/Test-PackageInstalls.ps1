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

$rows = foreach ($pkg in $packages) {
    if ($env:GITHUB_ACTIONS) { Write-Host "::group::pip install $pkg" }
    else { Write-Host "===== pip install $pkg =====" }

    # The install report contains the resolved download URL for the requested package
    $cmd = "python -m pip install --report C:\report.json $pkg; " +
           "if (`$LASTEXITCODE -eq 0) { " +
           "`$r = Get-Content C:\report.json -Raw | ConvertFrom-Json; " +
           "`$u = (`$r.install | Where-Object { `$_.requested } | ForEach-Object { `$_.download_info.url }) -join ' '; " +
           "Write-Output ('RESULT::success::' + `$u) } " +
           "else { Write-Output 'RESULT::failure::' }"
    $output = & $DockerExe run --rm $Image powershell -NoProfile -Command $cmd
    $output | Write-Host

    if ($env:GITHUB_ACTIONS) { Write-Host '::endgroup::' }

    $result = ($output | Where-Object { $_ -match '^RESULT::' } | Select-Object -Last 1) -replace '^RESULT::', ''
    if (-not $result) { $result = 'failure::' }
    $status, $url = $result -split '::', 2
    [pscustomobject]@{
        package                          = $pkg
        "${ColumnPrefix}_install_status" = $status
        "${ColumnPrefix}_binary_url"     = $url
    }
}

$rows | Export-Csv -Path $OutputPath -NoTypeInformation
Write-Host "Wrote $OutputPath"
