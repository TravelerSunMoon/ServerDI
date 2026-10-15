@echo off
rem Server for several PCs: listens on every network (LAN, VPN), TCP port 85.
rem Point the players' game at http://<this PC's address>:85 (22 characters at most).
rem public_ip.txt (optional): the address the other players use to reach this PC (e.g. a VPN address);
rem needed when someone also plays on this PC with the game pointing at 127.0.0.1.
rem Windows may ask for a firewall permission for Python: allow it.
cd /d "%~dp0"
set "PY=py -3"
where py >nul 2>nul || set "PY=python"
set "PUB="
if exist "%~dp0public_ip.txt" set /p PUB=<"%~dp0public_ip.txt"
if defined PUB (
  %PY% -u di_server.py --host 0.0.0.0 --port 85 --allow-login --public-ip %PUB% %*
) else (
  %PY% -u di_server.py --host 0.0.0.0 --port 85 --allow-login %*
)
pause
