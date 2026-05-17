@echo off
chcp 65001 >nul
title 新版本 (重构) — 测试中，出问题可切回老版本

REM ============================================================
REM SiRod Inspector 新版本启动器
REM   部署位置：D:\SiliconRod_v2\
REM   出问题切回老版本：双击桌面 老版本.bat
REM ============================================================

REM 进入新版本根目录（按现场部署路径调整）
cd /d D:\SiliconRod_v2
if errorlevel 1 (
    echo ERROR: 找不到新版本目录 D:\SiliconRod_v2
    echo 请确认代码已部署到该路径，或修改本脚本里的 cd 路径
    pause
    exit /b 1
)

REM 检查老版本是否还在跑（防止抢相机 / 串口）
tasklist /FI "IMAGENAME eq python.exe" 2>nul | find /I "python.exe" >nul
if not errorlevel 1 (
    echo.
    echo ============================================
    echo 检测到已有 python.exe 在跑
    echo 可能是老版本，或是上次没关干净的新版本
    echo 请到任务管理器结束所有 python.exe 后再启动
    echo ============================================
    echo.
    pause
    exit /b 2
)

echo.
echo ============================================
echo  SiRod Inspector — 新版本 启动中
echo  存图位置：D:\SiRod_v2\
echo  日志位置：%CD%\sirod_inspector\logs\
echo ============================================
echo.

python sirod_inspector\main_camera.py

REM 程序退出后不立即关 cmd 窗口，让操作员看清退出消息
echo.
echo 程序已退出。按任意键关闭窗口。
pause >nul
