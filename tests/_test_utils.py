"""
测试脚本共享：Windows UTF-8 控制台修复 + 编解码工具。

Windows PowerShell / cmd 默认代码页是 cp936 (GBK)。即使 Python 把
sys.stdout 切到 UTF-8，控制台仍按 GBK 解读字节 → 中文显示为 ??/乱码。
解决：进程启动时调 SetConsoleOutputCP(65001) 把当前控制台切到 UTF-8。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np


def setup_console_utf8() -> None:
    """让 Windows 控制台显示 UTF-8（一次性、对当前进程生效）。

    在脚本最顶部调用即可。失败静默忽略（非 Windows / 无 console）。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # 65001 = CP_UTF8
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 给子进程的环境变量（如有需要）
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def imwrite_safe(path: Path | str, img: np.ndarray, ext: str = ".bmp") -> bool:
    """cv2.imwrite 不支持 Windows 上含中文路径，用 imencode + 文件流写。"""
    import cv2
    path = Path(path)
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(buf.tobytes())
    return True


def imread_safe(path: Path | str, flags: int = -1) -> np.ndarray | None:
    """cv2.imread 不支持含中文路径时的替代实现。"""
    import cv2
    path = Path(path)
    if not path.is_file():
        return None
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, flags)
