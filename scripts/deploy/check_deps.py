"""
盐城部署环境检查脚本
=====================
跑一遍打印每项的红绿灯，定位到底缺什么。

用法::
    python scripts/deploy/check_deps.py
    或双击 scripts/deploy/check_deps.bat

设计约束：纯标准库，不能 import 任何第三方包 —— 因为这个脚本要在
依赖装好"之前"也能跑（用来检查依赖装没装）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# ── 颜色（Windows 10+ 终端支持 ANSI）──
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# Windows 终端 ANSI 启用
if os.name == "nt":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        RED = GREEN = YELLOW = RESET = ""

_errors = 0
_warns = 0


def ok(msg: str) -> None:
    print(f"  {GREEN}[OK]{RESET} {msg}")


def fail(msg: str) -> None:
    global _errors
    _errors += 1
    print(f"  {RED}[X ]{RESET} {msg}")


def warn(msg: str) -> None:
    global _warns
    _warns += 1
    print(f"  {YELLOW}[! ]{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n[{title}]")


# ============================================================
# 检查项
# ============================================================

def check_python() -> None:
    section("1/7 Python 版本")
    v = sys.version_info
    print(f"  当前: Python {v.major}.{v.minor}.{v.micro}")
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor} >= 3.10")
    else:
        fail(f"Python {v.major}.{v.minor} 过低，需要 3.10+")


def check_long_path() -> None:
    section("2/7 Windows 长路径开关")
    if os.name != "nt":
        warn("非 Windows 平台，跳过")
        return
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        )
        val, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
        winreg.CloseKey(key)
        if val == 1:
            ok("LongPathsEnabled = 1")
        else:
            warn(f"LongPathsEnabled = {val} （建议改 1，深目录可能截断）")
    except FileNotFoundError:
        warn("注册表项不存在（建议设 LongPathsEnabled=1 后重启）")
    except Exception as e:
        warn(f"读取注册表失败: {e}")


def check_bv_camera() -> None:
    section("3/7 BV 相机驱动")
    candidates = [
        Path(r"C:\Program Files\Bluevision\BVCam\Driver\BVCam.dll"),
        Path(r"C:\Program Files\Bluevision\BVCam"),
    ]
    found = False
    for p in candidates:
        if p.exists():
            ok(f"{p}")
            found = True
            break
    if not found:
        fail("BVCam 驱动未装：装 BVCam 客户端到 C:\\Program Files\\Bluevision\\BVCam\\")


def check_ai_dll() -> None:
    section("4/7 EasyLabel AI 推理 DLL")
    base = Path(r"D:\EasyLabel_x64\DeepLearning")
    if not base.is_dir():
        fail(f"目录不存在: {base}")
        fail("装 MvitSDK_4.1.23.622.exe（默认路径），或联系开发改 init_runtime(dll_dir=...)")
        return

    for f in ("dnninfer.dll", "dnndefine.dll"):
        p = base / f
        if p.is_file():
            ok(f"{p}")
        else:
            fail(f"缺失: {p}")

    # 加密狗 / 授权：DLL 文件存在但授权不到位时，IsDllValid 会返回 False
    # 这里不真去 LoadLibrary（会 chdir 不可逆），仅提示
    print(f"  {YELLOW}注：DLL 文件存在 != 授权 OK；首次启动会再校验授权（加密狗 / license）{RESET}")


def check_python_packages() -> None:
    section("5/7 Python 第三方包")
    # (import 名, pip 名)
    packages = [
        ("PyQt6", "PyQt6"),
        ("numpy", "numpy"),
        ("cv2", "opencv-python"),
        ("PIL", "Pillow"),
        ("pymysql", "pymysql"),
        ("requests", "requests"),
        ("serial", "pyserial"),
        ("psutil", "psutil"),
        ("matplotlib", "matplotlib"),
        ("openpyxl", "openpyxl"),
        ("cryptography", "cryptography"),   # 软件授权锁 license_guard 验签用
    ]
    missing = []
    for import_name, pip_name in packages:
        spec = importlib.util.find_spec(import_name)
        if spec is not None:
            ok(f"{import_name}  ({pip_name})")
        else:
            fail(f"{import_name}  (pip install {pip_name})")
            missing.append(pip_name)

    if missing:
        cmd = "pip install -i https://pypi.tuna.tsinghua.edu.cn/simple " + " ".join(missing)
        print(f"\n  {YELLOW}一键补装：{RESET}")
        print(f"    {cmd}")


def check_models() -> None:
    section("6/7 AI 模型文件")
    # check_deps.py 在 scripts/deploy/ 下，项目根目录是 ../..
    root = Path(__file__).resolve().parent.parent.parent
    for name in ("Model_seg.m", "Model_cls.m"):
        p = root / "models" / name
        if p.is_file():
            size_mb = p.stat().st_size / 1024 / 1024
            ok(f"models/{name}  ({size_mb:.1f} MB)")
        else:
            fail(f"缺失: {p}")


def check_config() -> None:
    section("7/7 config.json")
    root = Path(__file__).resolve().parent.parent.parent
    cfg = root / "sirod_inspector" / "config.json"
    example = root / "sirod_inspector" / "config.v2.example.json"

    if cfg.is_file():
        ok(f"sirod_inspector/config.json  ({cfg.stat().st_size} bytes)")
        # 简单 sanity check：能不能解析、关键字段是否还是模板占位
        try:
            import json
            data = json.loads(cfg.read_text(encoding="utf-8"))
            host = data.get("database", {}).get("host", "")
            pwd = data.get("database", {}).get("password", "")
            if "改成现场" in host or "改成现场" in pwd or "现场" in host:
                warn("config.json 数据库 host/password 看起来还是模板占位，请改成现场值")
        except Exception as e:
            fail(f"config.json 解析失败: {e}")
    else:
        if example.is_file():
            warn(f"未拷贝 config.json — 复制 {example.name} 并改现场值")
        else:
            fail("既无 config.json 也无 config.v2.example.json，部署包不完整")


# ============================================================
# 主流程
# ============================================================

def main() -> int:
    print("=" * 60)
    print("  SiRod Inspector 部署环境检查")
    print("=" * 60)

    check_python()
    check_long_path()
    check_bv_camera()
    check_ai_dll()
    check_python_packages()
    check_models()
    check_config()

    print()
    print("=" * 60)
    if _errors > 0:
        print(f"  {RED}[失败] {_errors} 项错误，{_warns} 项警告 — 修复错误后再启动{RESET}")
        result = 1
    elif _warns > 0:
        print(f"  {YELLOW}[部分通过] 无错误，{_warns} 项警告 — 注意但不阻塞启动{RESET}")
        result = 0
    else:
        print(f"  {GREEN}[全部通过] 可以双击 新版本.bat 启动了{RESET}")
        result = 0
    print("=" * 60)
    return result


if __name__ == "__main__":
    sys.exit(main())
