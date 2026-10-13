<#
.SYNOPSIS
Test pip installation of a list of packages, each in a fresh Docker container
or a fresh virtual environment on the host, and write the results to a CSV file.
Progress is also appended to a Markdown log after each package, so the status
can be checked while the script is running.

.EXAMPLE
./scripts/Test-PackageInstalls.ps1 -ColumnPrefix py3.14 -Image python-arm64

.EXAMPLE
./scripts/Test-PackageInstalls.ps1 -ColumnPrefix py3.14_buildtools -Python python
#>
[CmdletBinding(DefaultParameterSetName = 'Docker')]
param(
    # Column prefix for the CSV, e.g. 'py3.14' or 'py3.14_buildtools'
    [Parameter(Mandatory)]
    [string]$ColumnPrefix,

    [Parameter(Mandatory)]
    [string]$ListPath,

    # Docker image to run each install in
    [Parameter(ParameterSetName = 'Docker')]
    [string]$Image = 'python-arm64',

    # Host Python used to create a fresh venv for each install, instead of Docker
    [Parameter(Mandatory, ParameterSetName = 'Host')]
    [string]$Python,

    # Directory used as pip's cache. Keyed on the column prefix so that wheels
    # built for one variant are never reused by another.
    [string]$CacheDir,

    # Fail instead of falling back to a source build; for images without build tools
    [switch]$OnlyBinary,

    # Stop starting new installs after this many hours (0 = no limit). Defaults to 4
    # under Actions so results are still written before the 6 hour job timeout.
    [double]$TimeLimitHours = $(if ($env:GITHUB_ACTIONS) { 4 } else { 0 }),

    [string]$OutputPath,

    # Markdown progress log, updated after each package
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'
if (-not $OutputPath) { $OutputPath = "results-$ColumnPrefix.csv" }
if (-not $LogPath) { $LogPath = "results-$ColumnPrefix.md" }
if (-not $CacheDir) { $CacheDir = Join-Path ([IO.Path]::GetTempPath()) "pip-cache-$ColumnPrefix" }
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

$deadline = if ($TimeLimitHours -gt 0) { (Get-Date).AddHours($TimeLimitHours) } else { [datetime]::MaxValue }

$useHost = $PSCmdlet.ParameterSetName -eq 'Host'

$packages = @(Get-Content $ListPath | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })

# Receives pip's install report; mounted at C:\out when running in a container
$reportDir = Join-Path ([IO.Path]::GetTempPath()) "pip-reports-$PID"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$reportPath = Join-Path $reportDir 'report.json'

$pipArgs = @('install', '--no-warn-script-location')
if ($OnlyBinary) { $pipArgs += '--only-binary=:all:' }
if ($useHost) {
    $Python = (Get-Command $Python).Source
    $pyVersion = & $Python -c 'import sys; print(sys.version)'
    if ($LASTEXITCODE -ne 0) { throw "Python at $Python failed to run (exit code $LASTEXITCODE)" }
    $venvDir = Join-Path ([IO.Path]::GetTempPath()) "pip-venv-$PID"
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    $pipArgs += '--report', $reportPath, '--cache-dir', $CacheDir, '--disable-pip-version-check'
    $target = "Host Python: ``$Python`` ($pyVersion)"
}
else {
    $pipArgs += '--report', 'C:\out\report.json'
    $target = "Image: ``$Image``"
}

$rows = @()

function Format-MdCell([string]$Text) { $Text -replace '\|', '\|' }

$startTime = Get-Date
Set-Content -Path $LogPath -Encoding utf8 -Value @(
    "# Package install results: $ColumnPrefix"
    ''
    "- $target"
    "- List: ``$ListPath`` ($($packages.Count) packages)"
    "- Only binary: $([bool]$OnlyBinary)"
    "- Started: $($startTime.ToUniversalTime().ToString('u'))"
    ''
    '| # | Package | Status | Duration | Binary URL |'
    '|--:|---------|--------|---------:|------------|'
)

$done = 0
$succeeded = 0
try {
    foreach ($pkg in $packages) {
        if ((Get-Date) -gt $deadline) {
            $msg = "Time limit of $TimeLimitHours h reached; skipping $($packages.Count - $done) remaining packages."
            Write-Warning $msg
            Add-Content -Path $LogPath -Encoding utf8 -Value @('', "> **Warning:** $msg")
            break
        }
        $done++
        $pkgStart = Get-Date

        if ($env:GITHUB_ACTIONS) { Write-Host "::group::pip install $pkg" }
        else { Write-Host "===== pip install $pkg =====" }

        Remove-Item $reportPath -ErrorAction SilentlyContinue
        if ($useHost) {
            Remove-Item $venvDir -Recurse -Force -ErrorAction SilentlyContinue
            & $Python -m venv --clear $venvDir
            if ($LASTEXITCODE -eq 0) { & $venvPython -m pip @pipArgs $pkg }
            else { Write-Warning "Failed to create venv at $venvDir with exit code $LASTEXITCODE" }
        }
        else {
            docker run --rm -v "${reportDir}:C:\out" -v "${CacheDir}:C:\pip-cache" $Image python -m pip @pipArgs $pkg
        }

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
        $rows += [pscustomobject]@{
            package                          = $pkg
            "${ColumnPrefix}_install_status" = $status
            "${ColumnPrefix}_binary_url"     = $url
        }

        if ($status -eq 'success') { $succeeded++ }
        $icon = [char]::ConvertFromUtf32($(if ($status -eq 'success') { 0x2705 } else { 0x274C }))
        $duration = '{0:hh\:mm\:ss}' -f ((Get-Date) - $pkgStart)
        Add-Content -Path $LogPath -Encoding utf8 -Value (
            "| $done/$($packages.Count) | ``$(Format-MdCell $pkg)`` | $icon $status | $duration | $(Format-MdCell $url) |"
        )
    }
}
finally {
    $elapsed = '{0:hh\:mm\:ss}' -f ((Get-Date) - $startTime)
    Add-Content -Path $LogPath -Encoding utf8 -Value @(
        ''
        "Finished: $((Get-Date).ToUniversalTime().ToString('u')) (elapsed $elapsed). " +
        "$done of $($packages.Count) packages tested: $succeeded succeeded, $($done - $succeeded) failed."
    )
    Remove-Item $reportDir -Recurse -Force -ErrorAction SilentlyContinue
    if ($useHost) { Remove-Item $venvDir -Recurse -Force -ErrorAction SilentlyContinue }
    $global:LASTEXITCODE = 0
}

$rows | Export-Csv -Path $OutputPath -NoTypeInformation
Write-Host "Wrote $OutputPath ($done of $($packages.Count) packages)"
