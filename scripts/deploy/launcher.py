"""
SiRod Inspector 启动器 / watchdog（新版本.vbs 的 Python 移植版）
================================================================
职责（与原 新版本.vbs 一致）：
  - 隐藏窗口启动 pythonw sirod_inspector/main_camera.py，并等待其退出
  - 进程异常退出时自动重启
  - 短时间（< MIN_LIFETIME_SEC）内连崩 MAX_RESTARTS 次 → 放弃重启 + 弹框
  - 用户正常关闭 UI（rc=0）→ launcher 一并退出

打包成 exe：
    pyinstaller --onefile --noconsole --name SiRodLauncher scripts/deploy/launcher.py
产出 dist/SiRodLauncher.exe，放到 <项目根>/scripts/deploy/ 下双击即可。

约束：只用标准库。launcher 不能依赖 PyQt / 第三方包 ——
否则依赖坏掉时连"弹框报错"都做不到。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── watchdog 策略参数（与 新版本.vbs 保持一致）──
MAX_RESTARTS = 5          # 短命崩溃累计上限
MIN_LIFETIME_SEC = 30     # 运行不到 30s 就算"短命崩溃"
RESTART_DELAY_SEC = 5     # 崩溃后等几秒再重启

# 被 watchdog 拉起的目标（相对项目根）
TARGET_REL = Path("sirod_inspector") / "main_camera.py"


def project_root() -> Path:
    """项目根目录。

    launcher 住在 <根>/scripts/deploy/ 下，所以根 = 自身往上三级。
    冻结成 exe 时用 sys.executable（指向 exe 本体，不是 PyInstaller 临时目录）；
    源码方式运行时用 __file__。
    """
    base = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    return base.resolve().parent.parent.parent


PROJECT_DIR = project_root()
LOG_FILE = PROJECT_DIR / "sirod_inspector" / "logs" / "launcher.log"


def log_line(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        # 日志写不进去也不能让 watchdog 崩掉
        pass


def find_python() -> str | None:
    """优先 pythonw（无控制台窗口），退化到 python。找不到返回 None。"""
    for name in ("pythonw", "python"):
        path = shutil.which(name)
        if path:
            return path
    return None


def message_box(text: str, title: str) -> None:
    """纯 ctypes 弹框，不依赖任何 GUI 库。非 Windows 平台退化到 stderr。"""
    try:
        import ctypes
        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
        flags = 0x0 | 0x10 | 0x10000 | 0x40000
        ctypes.windll.user32.MessageBoxW(0, text, title, flags)
    except Exception:
        print(f"[{title}] {text}", file=sys.stderr)


def _child_env() -> dict:
    """子进程环境 —— 摘掉 PyInstaller 注入 PATH 的 _MEIPASS 临时目录。

    冻结成 onefile exe 后，PyInstaller 把解包临时目录 sys._MEIPASS 塞到 PATH
    最前面。子进程 pythonw 继承后，加载原生 DLL（如 EasyLabel 推理 DLL）时会
    优先撞上 bundle 自带的 VC 运行时等依赖 → 版本不符 → “无法定位程序输入点”。
    这里把 _MEIPASS 从 PATH 摘掉，让子进程拿到和命令行/旧 vbs 一致的干净环境。
    源码方式运行（无 _MEIPASS）时原样返回。
    """
    env = os.environ.copy()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        kept = [p for p in env.get("PATH", "").split(os.pathsep)
                if p and os.path.normpath(p) != os.path.normpath(meipass)]
        env["PATH"] = os.pathsep.join(kept)
    return env


def run_once(python_exe: str) -> tuple[int, int]:
    """启动一次目标进程，阻塞到它退出。返回 (退出码, 运行秒数)。"""
    start = time.monotonic()
    # CREATE_NO_WINDOW：即便退化用了 python.exe 也不闪控制台
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [python_exe, str(PROJECT_DIR / TARGET_REL)],
        cwd=str(PROJECT_DIR),
        creationflags=flags,
        env=_child_env(),
    )
    lifetime = int(time.monotonic() - start)
    return proc.returncode, lifetime


def classify_exit(returncode: int, lifetime: int, restart_count: int) -> tuple[str, int]:
    """根据退出码与存活时长决定下一步。

    返回 (action, new_restart_count)，action ∈ {"stop_clean", "give_up", "restart"}。
    逻辑与 新版本.vbs 完全对应。
    """
    if returncode == 0:
        return "stop_clean", restart_count
    if lifetime < MIN_LIFETIME_SEC:
        restart_count += 1
        if restart_count >= MAX_RESTARTS:
            return "give_up", restart_count
        return "restart", restart_count
    # 运行够久才崩 → 不算短命，重置累计
    return "restart", 0


def main() -> int:
    log_line("================ Launcher 启动 ================")
    log_line(f"项目目录: {PROJECT_DIR}")

    python_exe = find_python()
    if python_exe is None:
        msg = "找不到 pythonw/python，无法启动 — 请确认 Python 已装且在 PATH"
        log_line(msg)
        message_box(msg, "SiRod Inspector 启动失败")
        return 1
    log_line(f"使用解释器: {python_exe}")

    restart_count = 0
    while True:
        log_line(f"启动 main_camera.py (累计重启 {restart_count} 次)")
        rc, lifetime = run_once(python_exe)
        log_line(f"main_camera 退出 rc={rc} 运行时长={lifetime}s")

        action, restart_count = classify_exit(rc, lifetime, restart_count)

        if action == "stop_clean":
            log_line("正常退出，launcher 也退出")
            break

        if action == "give_up":
            log_line("连续短命崩溃达上限，放弃重启，弹消息")
            message_box(
                f"SiRod Inspector 短时间内连续崩溃 {MAX_RESTARTS} 次，已停止自动重启。\n\n"
                "请检查日志：\n"
                f"{PROJECT_DIR}\\sirod_inspector\\logs\\sirod_inspector.log\n"
                f"{PROJECT_DIR}\\sirod_inspector\\logs\\sirod_error.log\n\n"
                "或联系开发。",
                "SiRod Inspector 启动失败",
            )
            break

        # action == "restart"
        if lifetime < MIN_LIFETIME_SEC:
            log_line(f"短命崩溃 ({lifetime}s) 累计 {restart_count}/{MAX_RESTARTS}")
        else:
            log_line(f"运行 {lifetime}s 后崩溃（不算短命），重置计数")
        log_line(f"等 {RESTART_DELAY_SEC} 秒后重启...")
        time.sleep(RESTART_DELAY_SEC)

    log_line("================ Launcher 退出 ================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
