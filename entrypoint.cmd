@echo off
if exist "C:\BuildTools\Common7\Tools\VsDevCmd.bat" call "C:\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=arm64 -host_arch=arm64 -no_logo
%*
