# Builds dinet_bk.dll (32-bit) with Visual Studio 2022, calling cl.exe directly (same options as build.bat,
# for shells where vcvars32.bat / cmd.exe cannot run).
$ErrorActionPreference = 'Stop'
$s = $PSScriptRoot
$vs = 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC'
$vc = (Get-ChildItem $vs -Directory | Sort-Object Name | Select-Object -Last 1).FullName
$k = 'C:\Program Files (x86)\Windows Kits\10'
$v = (Get-ChildItem "$k\Include" -Directory | Sort-Object Name | Select-Object -Last 1).Name
$env:INCLUDE = "$vc\include;$k\Include\$v\ucrt;$k\Include\$v\um;$k\Include\$v\shared"
$env:LIB = "$vc\lib\x86;$k\Lib\$v\ucrt\x86;$k\Lib\$v\um\x86"
$env:PATH = "$vc\bin\Hostx64\x64;$env:PATH"
& "$vc\bin\Hostx64\x86\cl.exe" /nologo /O2 /MT /W3 /LD "$s\dinet.c" "/Fe:$s\dinet_bk.dll" "/Fo:$s\dinet.obj" `
    /link /DLL /NODEFAULTLIB:libcmtd user32.lib kernel32.lib advapi32.lib ws2_32.lib winhttp.lib
if ($LASTEXITCODE) { exit $LASTEXITCODE }
Remove-Item -LiteralPath "$s\dinet.obj", "$s\dinet_bk.exp" -ErrorAction SilentlyContinue
Write-Host "Built $s\dinet_bk.dll"
