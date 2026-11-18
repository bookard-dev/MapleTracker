@echo off
setlocal EnableDelayedExpansion
title MapleTracker - First Time Setup
cd /d "%~dp0"
echo.
echo  MapleTracker - first time setup
echo  ===============================
echo  This finds Python (3.10 or newer), installs it if it's missing, then
echo  installs the packages MapleTracker needs. Run it again any time to repair.
echo.

call :find_python
if defined PY goto have_python

echo  Python 3.10 or newer wasn't found on this computer.
echo.
where winget >nul 2>nul
if errorlevel 1 goto manual_python
choice /c YN /m " Install Python 3.12 now (from Microsoft's winget, just for your user)"
if errorlevel 2 goto manual_python
echo.
winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto have_python
echo.
echo  Python was installed, but this window can't see it yet.
echo  Close this window and run "First Time Setup.bat" again.
goto end

:manual_python
echo.
echo  Please install Python yourself:
echo    1. The download page opens now: get the latest Python 3 for Windows.
echo    2. In the installer, tick "Add python.exe to PATH", then Install Now.
echo    3. Run "First Time Setup.bat" again.
start "" https://www.python.org/downloads/windows/
goto end

:have_python
echo  Using Python: !PY!
"!PY!" -c "import sys; print('  version', sys.version.split()[0])"
"!PY!" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo.
    echo  This Python has no tkinter ^(the window toolkit^). Reinstall Python from
    echo  python.org with "tcl/tk and IDLE" ticked, then run this again.
    goto end
)
echo.
echo  Installing packages ^(this can take a few minutes the first time^)...
"!PY!" -m pip install --disable-pip-version-check --upgrade pip >nul 2>nul
"!PY!" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo  Installing the packages failed - see the messages above.
    echo  Check your internet connection and run this again.
    goto end
)
echo.
echo  Checking everything loads...
"!PY!" -c "import mss, numpy, cv2, PIL, tkinter; from winrt.windows.media.ocr import OcrEngine; from winrt.windows.graphics.imaging import SoftwareBitmap; from winrt.windows.storage.streams import DataWriter; from winrt.windows.globalization import Language"
if errorlevel 1 (
    echo.
    echo  Something didn't load - see the error above.
    goto end
)
echo.
echo  All set. Start MapleTracker with "Start MapleTracker.bat".
goto end

rem ---------------------------------------------------------------- find_python
rem Sets PY to a python.exe that is 3.10 or newer, or leaves it empty.
:find_python
set "PY="
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do call :try_python "%%P"
if defined PY exit /b
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY call :try_python "%%P"
if defined PY exit /b
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "C:\Python3*") do if not defined PY call :try_python "%%D\python.exe"
exit /b

:try_python
if not exist "%~1" exit /b
echo %~1 | find /i "WindowsApps" >nul && exit /b
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b

:end
echo.
pause
