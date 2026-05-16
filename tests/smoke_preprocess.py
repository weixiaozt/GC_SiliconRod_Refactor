"""
预处理烟雾测试
=================
跑一遍 source_image/*.tif → 预处理 → 写到 tests/outputs/preprocess/，
并和 test_image/ 下的目标效果做尺寸 / 灰度分布对比。

用法
----
    python tests/smoke_preprocess.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imwrite_safe, imread_safe
setup_console_utf8()

import cv2
import numpy as np

from sirod_inspector.algorithm import preprocess_with_debug


def stats(arr: np.ndarray, name: str) -> str:
    return (f"{name}: shape={arr.shape} dtype={arr.dtype} "
            f"min={arr.min()} max={arr.max()} mean={arr.mean():.1f} "
            f"p50={np.median(arr):.0f} "
            f"p10={np.percentile(arr, 10):.0f} "
            f"p90={np.percentile(arr, 90):.0f}")


def main() -> int:
    src_dir = _REPO_ROOT / "source_image"
    test_dir = _REPO_ROOT / "test_image"
    out_dir = _REPO_ROOT / "tests" / "outputs" / "preprocess"
    out_dir.mkdir(parents=True, exist_ok=True)

    src_files = sorted(src_dir.glob("*.tif"))
    if not src_files:
        print(f"[FAIL] {src_dir} 下找不到 .tif 原图")
        return 1

    print("=" * 60)
    print("预处理烟雾测试")
    print("=" * 60)
    print(f"input dir : {src_dir} ({len(src_files)} 个 .tif)")
    print(f"output dir: {out_dir}")
    print()

    # 先打印参考图（test_image）的统计，作为目标对照
    print("─── 参考目标 (test_image/) ───")
    for p in sorted(test_dir.glob("*"))[:3]:
        img = imread_safe(p, cv2.IMREAD_UNCHANGED)
        if img is None:
            continue
        print(f"  {p.name}  {stats(img, '          ')}")
    print()

    print("─── 处理 source_image/ ───")
    for p in src_files:
        img = imread_safe(p, cv2.IMREAD_UNCHANGED)
        if img is None:
            print(f"  [SKIP] cv2.imread 失败: {p.name}")
            continue

        t0 = time.perf_counter()
        out, info = preprocess_with_debug(img)
        dt_ms = (time.perf_counter() - t0) * 1000

        print(f"\n  {p.name}  ({dt_ms:.0f} ms)")
        print(f"    input  : shape={info['input_shape']} dtype={info['input_dtype']} "
              f"min={info['input_min']} max={info['input_max']} "
              f"mean={info['input_mean']:.0f}")
        print(f"    clip V : {info['clip_V']:.0f}   clip_max={info['clip_max']:.0f}")
        print(f"    output : shape={info['output_shape']} dtype={info['output_dtype']} "
              f"min={info['output_min']} max={info['output_max']} "
              f"mean={info['output_mean']:.0f}")

        # 写盘以肉眼对照
        out_path = out_dir / f"{p.stem}_processed.bmp"
        imwrite_safe(out_path, out, ext=".bmp")

    print()
    print(f"[OK] 预处理完成，结果写到 {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
