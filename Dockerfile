# escape=`
ARG BASE_IMAGE=mcr.microsoft.com/windows/servercore:ltsc2025-arm64
FROM ${BASE_IMAGE}

SHELL ["powershell", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;"]

ARG PYTHON_VERSION=3.14
ARG PYMANAGER_VERSION=26.3
ARG INSTALL_VS_BUILDTOOLS=false

RUN New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -Value 1 -PropertyType DWORD -Force

RUN Invoke-WebRequest -Uri \"https://www.python.org/ftp/python/pymanager/python-manager-$env:PYMANAGER_VERSION.msi\" -OutFile C:\pymanager.msi; `
    $p = Start-Process msiexec.exe -ArgumentList '/i', 'C:\pymanager.msi', '/quiet', '/norestart' -Wait -PassThru; `
    if ($p.ExitCode -ne 0) { throw \"Python install manager MSI failed with exit code $($p.ExitCode)\" }; `
    Remove-Item C:\pymanager.msi

ENV PYTHON_MANAGER_DEFAULT=${PYTHON_VERSION}-arm64 `
    PYTHON_MANAGER_CONFIRM=no `
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN py install \"$env:PYTHON_VERSION-arm64\"; `
    py list; `
    setx /M PATH \"$env:LocalAppData\Python\bin;$env:PATH\"

RUN python -VV; python -m pip --version

RUN if ($env:INSTALL_VS_BUILDTOOLS -eq 'true') { `
      Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vs_buildtools.exe' -OutFile C:\vs_buildtools.exe; `
      $p = Start-Process C:\vs_buildtools.exe -ArgumentList `
        '--quiet', '--wait', '--norestart', '--nocache', `
        '--installPath', 'C:\BuildTools', `
        '--add', 'Microsoft.VisualStudio.Workload.VCTools', `
        '--add', 'Microsoft.VisualStudio.Component.VC.Tools.ARM64', `
        '--add', 'Microsoft.VisualStudio.Component.Windows11SDK.22621' `
        -Wait -PassThru; `
      if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw \"VS Build Tools install failed with exit code $($p.ExitCode)\" }; `
      Remove-Item C:\vs_buildtools.exe `
    } else { Write-Host 'Skipping VS Build Tools install' }

RUN $lines = @( `
      '@echo off', `
      'if exist \"C:\BuildTools\Common7\Tools\VsDevCmd.bat\" call \"C:\BuildTools\Common7\Tools\VsDevCmd.bat\" -arch=arm64 -host_arch=arm64 -no_logo', `
      '%*' `
    ); `
    Set-Content -Path C:\entrypoint.cmd -Value $lines -Encoding ascii

ENTRYPOINT ["cmd", "/S", "/C", "C:\\entrypoint.cmd"]
CMD ["powershell"]
