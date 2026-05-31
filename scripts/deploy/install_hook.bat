@echo off
chcp 65001 >nul
REM ============================================================
REM  方案B 一键安装：把 site 钩子锁装进"当前 PATH 上 python"的 site-packages。
REM  ★ 这个 python 必须是实际启动 main_camera.py 的那个 ★
REM  （launcher 用 PATH 上的 pythonw/python；不确定先 where python 看一眼）
REM  卸载：install_hook.bat --uninstall
REM ============================================================
python "%~dp0install_hook.py" %*
echo.
pause
