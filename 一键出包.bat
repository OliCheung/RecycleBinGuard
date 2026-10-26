@echo off
rem ============================================================================
rem  RecycleBinGuard - one-click build
rem
rem  Double-click to run: tests -> icon -> PyInstaller -> smoke check.
rem  Output: dist\RecycleBinGuard.exe
rem
rem  Optional args (drag this file onto a command prompt, or edit BUILD_ARGS):
rem    --skip-tests         skip the test step
rem    --release            tag the build in git
rem    --release --push     tag and push to origin
rem
rem  NOTE: this file is intentionally pure ASCII on purpose. cmd.exe parses the
rem  batch before `chcp` takes effect, so non-ASCII text here garbles on a
rem  GBK default codepage. All Chinese output comes from build.py instead.
rem ============================================================================
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "BUILD_ARGS=%*"
set "APP=RecycleBinGuard"

echo.
echo ============================================================
echo   %APP% - build
echo ============================================================
echo.

rem ---- 1) locate a usable Python -----------------------------------------
set "PY="
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    echo [1/3] using project venv: .venv
    goto :found_python
)
where py >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    echo [1/3] using py launcher
    goto :found_python
)
where python >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    echo [1/3] using python on PATH
    goto :found_python
)

echo [ERROR] Python not found.
echo         Install Python 3.11+ or create .venv in this folder.
echo.
pause
exit /b 1

:found_python

rem ---- 2) check build dependencies ---------------------------------------
%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller is missing.
    echo         Run: %PY% -m pip install -r requirements-dev.txt
    echo.
    pause
    exit /b 1
)
%PY% -c "import pytest" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] pytest is missing.
    echo         Run: %PY% -m pip install -r requirements-dev.txt
    echo.
    pause
    exit /b 1
)

rem ---- 3) run the build script -------------------------------------------
echo [2/3] checking for running instances...
echo [3/3] building, please wait (about 1-2 minutes)...
echo.

%PY% build.py %BUILD_ARGS%
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="0" (
    echo ============================================================
    echo   BUILD OK
    echo ============================================================
    if exist "dist\RecycleBinGuard.exe" start "" explorer /select,"%CD%\dist\RecycleBinGuard.exe"
) else if "%CODE%"=="2" (
    echo ============================================================
    echo   BUILD ABORTED: app is running, cannot overwrite the exe
    echo   Quit it from the tray menu first, then run this again
    echo ============================================================
) else (
    echo ============================================================
    echo   BUILD FAILED ^(exit code %CODE%^), see output above
    echo ============================================================
)

echo.
pause
exit /b %CODE%
