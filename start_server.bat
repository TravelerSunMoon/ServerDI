@echo off
rem Starts the server for this PC only (http://127.0.0.1:8085). Extra options are passed through,
rem e.g.  start_server.bat --game "C:\Program Files (x86)\Steam\steamapps\common\Disney Infinity 3.0 Gold Edition"
cd /d "%~dp0"
title Disney Infinity local server
py -3 di_server.py %*
if errorlevel 9009 python di_server.py %*
pause
