@echo off
chcp 65001 >nul
REM ============================================================
REM  编译 license_guard.py -> license_guard.<abi>.pyd  (tier B)
REM ------------------------------------------------------------
REM  把验签逻辑 + 内嵌公钥藏进二进制，现场只发 .pyd（删 .py），
REM  不再是记事本能改的明文。
REM
REM  前置：
REM    1) 已跑过 license_gen.py keygen，并把公钥粘进 license_guard.py
REM       （还是占位符会被本脚本拦下）
REM    2) pip install nuitka
REM    3) C 编译器：装了 VS Build Tools(MSVC) 最好；没有则加 --mingw64
REM       让 Nuitka 自动下 MinGW64
REM
REM  ★ 必须用与目标机相同的 Python 版本编译 ★
REM    .pyd 带 ABI 标签，cp310 只能在 Python 3.10 上加载、cp312 只能在 3.12 上。
REM    本 .bat 用当前 PATH 上的 python（本机 3.12 → cp312）。
REM    盐城是 Python 3.10 → 要 cp310，别用这个 .bat，用下面这条（--no-project 很关键，
REM    否则会把项目 .venv 重建成 3.10）：
REM      uv run --no-project --python 3.10 --with nuitka python -m nuitka --module ^
REM        sirod_inspector/core/license_guard.py --output-dir=build/license_guard_310 ^
REM        --remove-output --assume-yes-for-downloads
REM ============================================================

cd /d %~dp0\..\..

REM —— 防呆：公钥还是占位符就别编 ——
findstr /C:"PASTE_PUBLIC_KEY_HEX_HERE" sirod_inspector\core\license_guard.py >nul
if %errorlevel%==0 (
    echo [X] license_guard.py 还是占位公钥！
    echo     先跑：python tools\license_gen.py keygen
    echo     把打印的 _PUBLIC_KEY_HEX = "..." 整行覆盖进 license_guard.py，再来编译。
    exit /b 1
)

echo [*] 当前 Python 版本（要和生产一致）：
python --version

echo [*] 开始 Nuitka 编译 license_guard ...
python -m nuitka --module sirod_inspector\core\license_guard.py ^
    --output-dir=build\license_guard --remove-output --assume-yes-for-downloads
if %errorlevel% neq 0 (
    echo [X] 编译失败。若提示缺编译器，重跑并在上面命令末尾加 --mingw64
    exit /b 1
)

echo.
echo ============================================================
echo  [OK] 产物：build\license_guard\license_guard.*.pyd
echo  部署到现场：
echo    1) 把该 .pyd 拷到 sirod_inspector\core\
echo    2) 删除现场的 sirod_inspector\core\license_guard.py
echo    （只留 .pyd，源码态宽松放行的逻辑就不会出现在现场）
echo ============================================================
