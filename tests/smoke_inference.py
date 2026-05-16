"""
推理 DLL 烟雾测试
=================
验证 sirod_inspector.algorithm.inference 能正确加载 DLL、模型并跑一次推理。

用法
----
    # 自动查找模型 + 测试图（按下方默认路径列表查找）
    python tests/smoke_inference.py

    # 显式指定
    python tests/smoke_inference.py --cls path/to/Model_cls.m \\
                                     --seg path/to/Model_seg.m \\
                                     --image path/to/test.bmp

    # 仅测分类 / 仅测分割
    python tests/smoke_inference.py --only cls
    python tests/smoke_inference.py --only seg

退出码
------
    0  成功
    1  参数 / 文件不存在
    2  推理失败
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# ── 确保能 import sirod_inspector 包 ──
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imread_safe
setup_console_utf8()

try:
    import cv2
    import numpy as np
except ImportError as e:
    print(f"[FAIL] 缺少依赖: {e}。请 pip install opencv-python numpy")
    sys.exit(1)


# ── 默认模型 / 测试图候选位置（按顺序找第一个存在的）──
_MODEL_SEARCH_DIRS = [
    _REPO_ROOT / "models",
    _REPO_ROOT / "sirod_inspector" / "models",
    _REPO_ROOT / "algorithm",
    # 复用既有项目中的模型（便于现场快速 smoke test）
    Path(r"D:\Project\silicon-rod-defect-review\models"),
    Path(r"D:\Project\silicon-rod-defect-review\model"),
]
_IMAGE_SEARCH_DIRS = [
    _REPO_ROOT / "samples",
    _REPO_ROOT / "sirod_inspector" / "samples",
    Path(r"D:\Project\silicon-rod-defect-review\samples"),
    Path(r"D:\Project\silicon-rod-defect-review\source_image"),
]


def _find(filename_patterns: list[str], dirs: list[Path]) -> Path | None:
    for d in dirs:
        if not d.is_dir():
            continue
        for pat in filename_patterns:
            hits = sorted(d.rglob(pat))
            if hits:
                return hits[0]
    return None


def _resolve_paths(args) -> tuple[Path | None, Path | None, Path | None]:
    cls_path = Path(args.cls).resolve() if args.cls else _find(
        ["Model_cls.m"], _MODEL_SEARCH_DIRS
    )
    seg_path = Path(args.seg).resolve() if args.seg else _find(
        ["Model_seg.m"], _MODEL_SEARCH_DIRS
    )
    img_path = Path(args.image).resolve() if args.image else _find(
        ["*.bmp", "*.jpg", "*.png"], _IMAGE_SEARCH_DIRS
    )
    return cls_path, seg_path, img_path


def run_classifier(model_path: Path, img: np.ndarray) -> int:
    from sirod_inspector.algorithm import Classifier

    print(f"\n[CLS] 加载模型: {model_path}")
    t0 = time.perf_counter()
    with Classifier(model_path) as cls:
        t1 = time.perf_counter()
        print(f"      init  耗时 {1000 * (t1 - t0):.1f} ms")
        print(f"      输入  {cls.input_width}x{cls.input_height}x{cls.input_channels}")
        print(f"      类别  {cls.class_names}")

        t0 = time.perf_counter()
        result = cls.predict(img)
        t1 = time.perf_counter()
        print(f"      推理  耗时 {1000 * (t1 - t0):.1f} ms")
        print(f"      结果  label={result.label} ({result.name}) "
              f"conf={result.confidence:.4f}")
        for i, p in enumerate(result.probs):
            print(f"            {cls.class_names[i]:<10s} {p:.4f}")
    return 0


def run_segmenter(model_path: Path, img: np.ndarray) -> int:
    from sirod_inspector.algorithm import Segmenter

    print(f"\n[SEG] 加载模型: {model_path}")
    t0 = time.perf_counter()
    with Segmenter(model_path) as seg:
        t1 = time.perf_counter()
        print(f"      init  耗时 {1000 * (t1 - t0):.1f} ms")
        print(f"      输入  {seg.input_width}x{seg.input_height}x{seg.input_channels}")
        print(f"      类别  {seg.class_names}")

        t0 = time.perf_counter()
        result = seg.predict(img)
        t1 = time.perf_counter()
        print(f"      推理  耗时 {1000 * (t1 - t0):.1f} ms")
        print(f"      检出  {len(result.rects)} 个连通块")
        for i, r in enumerate(result.rects):
            print(f"            [{i}] {r.name:<10s} "
                  f"rect=({r.left},{r.top},{r.width}x{r.height}) "
                  f"area={r.area}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="推理 DLL 烟雾测试")
    ap.add_argument("--cls", help="分类模型路径 (.m)")
    ap.add_argument("--seg", help="分割模型路径 (.m)")
    ap.add_argument("--image", help="测试图片路径")
    ap.add_argument("--only", choices=["cls", "seg"],
                    help="只跑分类 / 只跑分割")
    args = ap.parse_args()

    cls_path, seg_path, img_path = _resolve_paths(args)

    print("=" * 60)
    print("推理 DLL 烟雾测试")
    print("=" * 60)
    print(f"cls   : {cls_path}")
    print(f"seg   : {seg_path}")
    print(f"image : {img_path}")

    if img_path is None or not img_path.is_file():
        print("\n[FAIL] 找不到测试图片。用 --image 指定，或把图放到："
              f"\n  {[str(d) for d in _IMAGE_SEARCH_DIRS]}")
        return 1

    img = imread_safe(img_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        print(f"[FAIL] cv2.imread 失败: {img_path}")
        return 1
    print(f"image 形状: {img.shape}  dtype: {img.dtype}")

    try:
        if args.only in (None, "cls"):
            if cls_path is None or not cls_path.is_file():
                print("\n[SKIP] 找不到分类模型，跳过 cls")
            else:
                run_classifier(cls_path, img)

        if args.only in (None, "seg"):
            if seg_path is None or not seg_path.is_file():
                print("\n[SKIP] 找不到分割模型，跳过 seg")
            else:
                run_segmenter(seg_path, img)
    except Exception as e:
        print(f"\n[FAIL] 推理过程异常: {e}")
        import traceback
        traceback.print_exc()
        return 2

    print("\n[OK] 烟雾测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
