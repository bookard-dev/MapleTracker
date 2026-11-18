@echo off
setlocal
cd /d "%~dp0"
call :find_python
if not defined PYW (
    echo.
    echo  Python 3.10 or newer wasn't found.
    echo  Run "First Time Setup.bat" first - it installs everything MapleTracker needs.
    echo.
    pause
    exit /b 1
)
"%PYW:pythonw.exe=python.exe%" -c "import mss, numpy, cv2, PIL, winrt.windows.media.ocr" >nul 2>nul
if errorlevel 1 (
    echo.
    echo  Some of MapleTracker's packages are missing.
    echo  Run "First Time Setup.bat" first - it installs everything MapleTracker needs.
    echo.
    pause
    exit /b 1
)
start "" "%PYW%" -u "%~dp0maple_tracker.py" 2> "%~dp0tracker_errors.txt"
exit /b 0

rem Sets PYW to the pythonw.exe of a Python 3.10 or newer, or leaves it empty.
:find_python
set "PYW="
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do call :try_python "%%P"
if defined PYW exit /b
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYW call :try_python "%%P"
if defined PYW exit /b
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "C:\Python3*") do if not defined PYW call :try_python "%%D\python.exe"
exit /b

:try_python
if not exist "%~1" exit /b
if not exist "%~dp1pythonw.exe" exit /b
echo %~1 | find /i "WindowsApps" >nul && exit /b
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYW=%~dp1pythonw.exe"
exit /b
