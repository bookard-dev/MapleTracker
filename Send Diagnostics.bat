@echo off
setlocal
title MapleTracker - Diagnostics
cd /d "%~dp0"
echo.
echo  MapleTracker diagnostics
echo  ========================
echo  Have MapleStory open in a map (not the login screen), with the minimap showing.
echo  This takes a screenshot of the game and saves what MapleTracker reads into a zip.
echo  Nothing is sent anywhere: you send the zip yourself.
echo.
set "PY="
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul ^| find /v /i "WindowsApps"') do if not defined PY set "PY=%%P"
if not defined PY (
    echo  Python wasn't found. Run "First Time Setup.bat" first.
    goto end
)
"%PY%" "%~dp0maple_tracker.py" --diagnose
:end
echo.
pause
