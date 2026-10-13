# escape=`
ARG BASE_IMAGE=mcr.microsoft.com/windows/servercore:ltsc2025-arm64
FROM ${BASE_IMAGE}

SHELL ["powershell", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;"]

ARG PYTHON_VERSION=3.14
ARG PYMANAGER_VERSION=26.3

RUN New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -Value 1 -PropertyType DWORD -Force

RUN Invoke-WebRequest -Uri \"https://www.python.org/ftp/python/pymanager/python-manager-$env:PYMANAGER_VERSION.msi\" -OutFile C:\pymanager.msi; `
    $p = Start-Process msiexec.exe -ArgumentList '/i', 'C:\pymanager.msi', '/quiet', '/norestart' -Wait -PassThru; `
    if ($p.ExitCode -ne 0) { throw \"Python install manager MSI failed with exit code $($p.ExitCode)\" }; `
    Remove-Item C:\pymanager.msi

ENV PYTHON_MANAGER_DEFAULT=${PYTHON_VERSION}-arm64 `
    PYTHON_MANAGER_CONFIRM=no `
    PIP_DISABLE_PIP_VERSION_CHECK=1 `
    PIP_CACHE_DIR=C:\pip-cache

RUN py install \"$env:PYTHON_VERSION-arm64\"; `
    py list; `
    setx /M PATH \"$env:LocalAppData\Python\bin;$env:PATH\"

RUN python -VV; python -m pip --version

CMD ["powershell"]
