@echo off
title SiRod Inspector - Dev Launcher (uv run)

REM ============================================================
REM Dev launcher: uses "uv run" (.venv + all deps).
REM Project root located via %~dp0 (relative, works in any clone).
REM Differs from the field-deploy bat in the same folder:
REM   field-deploy = system python + hardcoded D:\SiliconRod_v2
REM   dev_uv.bat   = dev machine, uv run + relative path (need uv + uv sync)
REM ============================================================

cd /d "%~dp0..\.."
if errorlevel 1 (
    echo [ERROR] Cannot locate project root
    pause
    exit /b 1
)

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv not found. Install: winget install astral-sh.uv  then run: uv sync
    pause
    exit /b 1
)

echo ============================================================
echo   SiRod Inspector - Dev Mode (uv run)
echo   Project root: %CD%
echo ------------------------------------------------------------
echo   No BVCam.dll/scanner/MySQL on dev box: camera engine
echo   degrades but the UI still opens. Red status lights = normal.
echo ============================================================
echo.

uv run sirod-camera

echo.
echo Exited. Press any key to close.
pause >nul