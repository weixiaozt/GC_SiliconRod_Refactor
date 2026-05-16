"""
overlay.color_for_label 跨进程稳定性回归
==========================================
之前用 ``hash(str)``，Python 3.3+ 默认每次启动 PYTHONHASHSEED 重随机化，
同一类别（"隐裂"、"崩边" 等）每次 Python 运行返回不同 BGR。客户看 marked 图
"为什么这次隐裂是绿，上次是橙"。

修复后 ``color_for_label`` 走 md5，跨进程稳定。
本测试：1) 当前进程多次调用一致；2) 子进程产出与本进程一致。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

from sirod_inspector.algorithm.overlay import color_for_label


_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  {'[OK ]' if cond else '[FAIL]'} {name}"
           + (f"  — {detail}" if detail else ""))
    if not cond:
        _fail += 1


def main() -> int:
    print("=" * 60)
    print("overlay.color_for_label 稳定性测试")
    print("=" * 60)

    # 1) 同进程多次调用同色
    classes = ["隐裂", "崩边", "凹坑", "杂质", "刀痕"]
    for c in classes:
        c1 = color_for_label(c)
        c2 = color_for_label(c)
        c3 = color_for_label(c)
        check(f"同进程一致 '{c}'", c1 == c2 == c3,
              f"got {c1}, {c2}, {c3}")

    # 2) int label 仍工作（向后兼容）
    for i in [0, 1, 5, 17]:
        c1 = color_for_label(i)
        c2 = color_for_label(i)
        check(f"int label {i} 一致", c1 == c2, f"got {c1}, {c2}")

    # 3) 不同类别色不同
    cols = [color_for_label(c) for c in classes]
    check(f"{len(classes)} 类全互异", len(set(cols)) == len(classes),
          f"got {cols}")

    # 4) 跨进程一致 — fork 子进程跑同样输入，比对
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "sys.path.insert(0, r'%s'); "
        "from sirod_inspector.algorithm.overlay import color_for_label; "
        "print(repr([color_for_label(c) for c in ['隐裂','崩边','凹坑']]))"
    ) % (str(_REPO_ROOT), str(_REPO_ROOT / "sirod_inspector"))
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, encoding="utf-8")
    expected = [color_for_label(c) for c in ["隐裂", "崩边", "凹坑"]]
    sub_out = r.stdout.strip()
    check(f"子进程产出一致", repr(expected) == sub_out,
          f"sub={sub_out}, exp={expected!r}")

    print()
    if _fail == 0:
        print("[OK] color_for_label 跨进程稳定")
        return 0
    print(f"[FAIL] {_fail} 个失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
