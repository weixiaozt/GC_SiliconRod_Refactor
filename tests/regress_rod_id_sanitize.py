"""
rod_id sanitize 单元测试（防扫码枪给奇怪字符破坏路径/写盘）
=============================================================
扫码枪可能给出含 / \\ : | ? * < > " 等 Windows 非法字符的字符串，
直接拿来作文件名会:
  - 创建意外子目录（'ABC/123' → 子目录 ABC + 文件 123_…）
  - 在 Windows 上 IOError（NTFS 拒绝 < > : " | ? * \\ /）
  - 路径含 \\x00 等控制字符崩溃
sanitize 把这些字符替换成 _。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

import numpy as np

from sirod_inspector.core.tcp_server import InspectData


_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  {'[OK ]' if cond else '[FAIL]'} {name}"
           + (f"  — {detail}" if detail else ""))
    if not cond:
        _fail += 1


def main() -> int:
    print("=" * 60)
    print("rod_id sanitize 测试")
    print("=" * 60)

    from main_camera import save_inspect_images

    cases = [
        # (rod_id, 应该有的安全文件名片段, 不应该出现的字符)
        ("ABC123",       "ABC123",       []),                       # 正常
        ("ABC/123",      "ABC_123",      ["/"]),                    # 路径分隔
        ("AB\\CD",       "AB_CD",        ["\\"]),                   # Windows 路径
        ("X:Y",          "X_Y",          [":"]),                    # 盘符
        ("A|B?C*D",      "A_B_C_D",      ["|", "?", "*"]),          # 通配符
        ("<file>",       "_file_",       ["<", ">"]),
        ("rod\twith\n",  "rod_with_",    ["\t", "\n"]),             # 控制字符
        ("..",           "NoRead",       ["."]),                    # 仅点/空 → 兜底
        ("",             "NoRead",       []),                       # 空 → 兜底
        ("正常中文",     "正常中文",     []),                       # 中文应保留
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        for rod_id, expected_frag, forbidden in cases:
            data = InspectData(
                rod_id=rod_id, result="OK",
                image=np.zeros((10, 10), dtype=np.uint8),
            )
            paths = save_inspect_images(
                data, None,
                base_dir=str(Path(tmpdir) / "images"),
                raw_tif_dir=str(Path(tmpdir) / "ImageRaw"),
                web_image_dir=str(Path(tmpdir) / "WebImage"),
                web_url_base="",
            )
            full_raw = paths.get('full_raw', '')
            stem = Path(full_raw).stem if full_raw else ''
            # 1. 期望片段在 stem 里
            ok_frag = (expected_frag in stem) or \
                       (expected_frag == "NoRead" and stem.startswith("NoRead_"))
            # 2. 禁止字符不在 stem 里
            ok_forbid = all(ch not in stem for ch in forbidden)
            # 3. 文件确实存在
            ok_file = Path(full_raw).is_file() if full_raw else False
            check(f"rod={rod_id!r}",
                  ok_frag and ok_forbid and ok_file,
                  f"stem={stem}")

    print()
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
