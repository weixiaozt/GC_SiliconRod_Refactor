"""
存图逻辑 demo（不需要相机，用 source_image 的 .tif 模拟）
============================================================
跑 source_image/*.tif 通过完整 Pipeline + InspectData adapter，
然后用 main_camera 同款 ``save_inspect_images`` 落盘 4 类图。

输出位置::

    tests/outputs/demo_save_images/<日期>/
        ├── full/raw/<OK|NG>/<棒号>_<ts>.bmp        ← 干净大图（训练用）
        ├── full/marked/<OK|NG>/<棒号>_<ts>.png     ← 叠 mask + 框（客户看）
        └── crops/<分类>/raw/<棒号>_<ts>_d<i>.bmp   ← 干净小图（按分类归档）
            crops/<分类>/marked/<棒号>_<ts>_d<i>.png

跑完后到 tests/outputs/demo_save_images 看效果。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imread_safe
setup_console_utf8()

import cv2

from sirod_inspector.algorithm import Pipeline, JudgeConfig
from sirod_inspector.core.inspect_engine import detection_to_inspect_data
# main_camera 顶层函数（不依赖 PyQt6）
# 但 main_camera.py 顶部 import PyQt6 — 需要绕过
# 把 save_inspect_images 单独导入会触发模块加载 → 加载 PyQt6
# 解决：用 importlib 跳过


def _load_save_fn():
    """从 main_camera.py 仅取 save_inspect_images，避免 import 整个模块"""
    # 直接 exec module body 的 save_inspect_images 部分太复杂
    # 改用直接 import — PyQt6 现在已经装上，所以可以 import
    from sirod_inspector.main_camera import save_inspect_images
    return save_inspect_images


def main() -> int:
    ap = argparse.ArgumentParser(description="存图逻辑 demo")
    ap.add_argument("--input-dir", default=str(_REPO_ROOT / "source_image"),
                    help="原图 .tif 目录")
    ap.add_argument("--also-test-image", action="store_true",
                    help="同时跑 test_image/*.bmp（已知有 NG 隐裂样本）")
    ap.add_argument("--out-dir", default=str(
        _REPO_ROOT / "tests" / "outputs" / "demo_save_images"))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    print("=" * 60)
    print("存图逻辑 demo")
    print("=" * 60)
    print(f"输出目录: {out_dir}")

    # 收集要跑的图
    targets = []
    in_dir = Path(args.input_dir)
    if in_dir.is_dir():
        targets += sorted(in_dir.glob("*.tif"))
        targets += sorted(in_dir.glob("*.bmp"))
    if args.also_test_image:
        ti = _REPO_ROOT / "test_image"
        if ti.is_dir():
            targets += sorted(ti.glob("*.bmp"))
            targets += sorted(ti.glob("*.jpg"))

    if not targets:
        print(f"[FAIL] {args.input_dir} 下没找到图")
        return 1
    print(f"找到 {len(targets)} 张图")

    save_fn = _load_save_fn()
    seg = _REPO_ROOT / "models" / "Model_seg.m"
    cls = _REPO_ROOT / "models" / "Model_cls.m"

    with Pipeline(str(seg), str(cls), JudgeConfig()) as pipe:
        for i, img_path in enumerate(targets, 1):
            print(f"\n[{i}/{len(targets)}] {img_path.name}")
            img = imread_safe(img_path, cv2.IMREAD_UNCHANGED)
            if img is None:
                print(f"  [SKIP] 读图失败")
                continue
            print(f"  shape={img.shape} dtype={img.dtype}")

            # 跑 Pipeline，开 keep_label_map + keep_crops + keep_raw_input
            # （跟 InspectEngine.trigger_once 内部一样）
            t0 = time.perf_counter()
            result = pipe.process(img,
                                   keep_processed_image=True,
                                   keep_crops=True,
                                   keep_label_map=True,
                                   keep_raw_input=True)
            dt = (time.perf_counter() - t0) * 1000
            print(f"  推理 {dt:.0f}ms  result={result.result}  "
                  f"type={result.defect_type or '-'}  count={result.defect_count}")

            # 装配 InspectData
            data = detection_to_inspect_data(
                result, rod_id=img_path.stem, inspect_id=i,
            )

            # 调存图逻辑（与 main_camera 一模一样）
            paths = save_fn(
                data, result,
                base_dir=str(out_dir),
                ng_trigger_classes={"隐裂"},
                raw_tif_dir=str(out_dir / "_ImageRaw"),
                web_image_dir=str(out_dir / "_WebImage"),
                web_url_base="http://10.32.50.220:8080",
            )
            for k, v in paths.items():
                if k == "crops_count":
                    print(f"    crops_count = {v}")
                else:
                    print(f"    {k:<12s} = {Path(v).relative_to(out_dir)}")

    print()
    print("=" * 60)
    print(f"[OK] 完成。打开 {out_dir} 看 4 类图：")
    print(f"  full/raw/    干净大图 (.bmp)")
    print(f"  full/marked/ 带 mask + 缺陷框大图 (.png)")
    print(f"  crops/<分类>/raw/    干净小图（按分类归档）")
    print(f"  crops/<分类>/marked/ 带标注小图")
    return 0


if __name__ == "__main__":
    sys.exit(main())
