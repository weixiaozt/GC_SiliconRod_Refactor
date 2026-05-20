@echo off
chcp 65001 >nul 2>&1
title SiRod Inspector deps check

cd /d "%~dp0..\..\"
if errorlevel 1 (
    echo.
    echo [X] cd failed
    goto :END
)

echo.
echo ============================================================
echo  Working directory: %CD%
echo ============================================================
echo.

python --version
if errorlevel 1 (
    echo.
    echo [X] Python not found in PATH
    echo     Install Python 3.10+ from https://www.python.org/downloads/
    goto :END
)

echo.
echo ============================================================
echo  Running check_deps.py ...
echo ============================================================
echo.

python scripts\deploy\check_deps.py
set RC=%ERRORLEVEL%

echo.
echo ============================================================
echo  check_deps.py exit code: %RC%
echo ============================================================

:END
echo.
echo.
echo ###  Press any key to close window  ###
echo.
pause
exit /b
