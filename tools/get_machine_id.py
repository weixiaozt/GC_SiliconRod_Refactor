"""
现场取机器码（发回开发签发授权用）
====================================
在工控机上跑这个，把打印的「机器码 blob」整行复制发给开发。

    python tools/get_machine_id.py

★ 直接按文件加载 license_guard（优先 .pyd，回退 .py），不 import sirod_inspector
包 —— 那个包的 __init__ 会拖进 numpy/cv2/相机/halcon 等重依赖，取机器码用不上，
且能在「依赖还没装全」的新机器上也跑得起来。本脚本不含任何授权逻辑。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "sirod_inspector" / "core"


def _load_guard():
    # 优先编译产物 .pyd（带版本 ABI 后缀的也认），回退源码 .py
    candidates = list(_CORE.glob("license_guard*.pyd")) + [_CORE / "license_guard.py"]
    for path in candidates:
        if path.exists():
            spec = importlib.util.spec_from_file_location("license_guard", str(path))
            mod = importlib.util.module_from_spec(spec)
            # 必须先登记到 sys.modules，否则模块内 @dataclass 解析 __module__ 时
            # 取到 None → 'NoneType' object has no attribute '__dict__'
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(
        f"没找到 license_guard(.pyd/.py)：{_CORE}")


if __name__ == "__main__":
    try:
        guard = _load_guard()
    except Exception as e:  # noqa: BLE001
        print(f"[X] 加载 license_guard 失败: {e}", file=sys.stderr)
        sys.exit(1)
    guard.print_fingerprint()
