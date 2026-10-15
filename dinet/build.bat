@echo off
rem Builds dinet_bk.dll (32-bit) with Visual Studio 2022.
setlocal
set VCVARS=C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars32.bat
if not exist "%VCVARS%" (
  echo Visual Studio 2022 x86 tools not found: %VCVARS%
  exit /b 1
)
call "%VCVARS%" >nul || exit /b 1
cd /d "%~dp0"
cl /nologo /O2 /MT /W3 /LD /Fe:dinet_bk.dll dinet.c /link /DLL /NODEFAULTLIB:libcmtd user32.lib kernel32.lib advapi32.lib ws2_32.lib winhttp.lib || exit /b 1
del /q dinet.obj dinet_bk.exp 2>nul
echo Built %~dp0dinet_bk.dll
