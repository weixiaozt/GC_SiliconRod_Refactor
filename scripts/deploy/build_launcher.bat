@echo off
REM ============================================================
REM Build scripts\deploy\launcher.py into a single-file, no-console exe
REM   Output: scripts\deploy\SiRodLauncher.exe
REM           (bundled into the deploy zip by build_zip.py)
REM
REM IMPORTANT: use "python -m PyInstaller", NOT the pyinstaller.exe on PATH.
REM   The PATH pyinstaller may point at a different/older Python
REM   (we hit Python 3.8.6 + PyInstaller 3.6 that way). -m forces the
REM   interpreter you call here (needs PyInstaller installed for it).
REM ============================================================

REM cd to project root (this script lives in scripts\deploy\)
cd /d "%~dp0..\.."

set "BTMP=%CD%\.build_launcher"

REM Optional custom icon: drop an .ico at scripts\deploy\launcher.ico and it
REM gets picked up automatically. No icon file -> plain exe, no error.
if exist "scripts\deploy\launcher.ico" (
    set "ICONOPT=--icon scripts\deploy\launcher.ico"
    echo Using icon: scripts\deploy\launcher.ico
) else (
    set "ICONOPT="
    echo No scripts\deploy\launcher.ico found - building without custom icon.
)

echo Building scripts\deploy\launcher.py -^> scripts\deploy\SiRodLauncher.exe ...
python -m PyInstaller --onefile --noconsole %ICONOPT% --name SiRodLauncher --distpath scripts\deploy --workpath "%BTMP%\build" --specpath "%BTMP%" scripts\deploy\launcher.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED - see errors above.
    pause
    exit /b 1
)

REM remove PyInstaller intermediates, keep only the exe
rmdir /s /q "%BTMP%" 2>nul

echo.
echo ============================================
echo  DONE: scripts\deploy\SiRodLauncher.exe
echo  Double-click it on the deploy machine (replaces the watchdog vbs)
echo ============================================
pause
