@echo off
REM ============================================================
REM  Plan-B one-click installer (site-hook software lock).
REM  Installs into the site-packages of the python on PATH.
REM  This python MUST be the one that actually launches main_camera.py.
REM  Uninstall:   install_hook.bat --uninstall
REM ============================================================
python "%~dp0install_hook.py" %*
echo.
pause
