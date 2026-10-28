@echo off
setlocal
cd /d "%~dp0"

set "PYRUN="
where py >nul 2>nul && set "PYRUN=py"
if not defined PYRUN (where python >nul 2>nul && set "PYRUN=python")
if not defined PYRUN (where python3 >nul 2>nul && set "PYRUN=python3")
if not defined PYRUN goto NOPY

echo Running: %PYRUN% app.py
%PYRUN% app.py 2> "%~dp0launch.log"
if errorlevel 1 goto FAILED

del "%~dp0launch.log" >nul 2>nul
exit /b 0

:NOPY
echo.
echo [ERROR] Python not found. Please install Python 3 first:
echo.
echo   1. Open: https://www.python.org/downloads/windows/
echo   2. Click "Latest Python 3 Release"
echo   3. In the Files table, download "Windows installer (64-bit)"
echo   4. IMPORTANT: tick [x] Add python.exe to PATH on the first screen
echo   5. Run this file again
echo.
start https://www.python.org/downloads/windows/
pause
exit /b 1

:FAILED
echo.
echo [ERROR] The program exited with an error. ---- launch.log ----
type "%~dp0launch.log"
echo -----------------------------------------------------------
echo.
pause
exit /b 1
