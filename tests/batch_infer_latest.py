"""
批量推理脚本 — 跑最新模型 + 最新测试图
=========================================
对 D:/Project/SiliconRod_refactor/test_image/最新测试图/ 下每张 BMP：
  1. 预处理（如果是 uint16；现场图已是 uint8 1024×3072，会自动跳过）
  2. Seg 推理
  3. 连通块筛选 + cls 分类（Pipeline.process 内部串起来）
  4. Judge 判定
  5. 输出：
     - tests/outputs/batch_latest/<rod>_marked.png      整图带框 + 类别名
     - tests/outputs/batch_latest/crops/<类别>/<rod>_<idx>.png   缺陷小图（按类分文件夹，方便训练）

完全用 sirod_inspector.algorithm.Pipeline，跟现场跑的是同一份代码。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

import cv2
import numpy as np

from sirod_inspector.algorithm import JudgeConfig, Pipeline
from sirod_inspector.algorithm.overlay import draw_marked_full


# ─────────── 配置 ───────────
PROJECT_ROOT = Path(r"D:/Project/SiliconRod_refactor")
MODEL_SEG = PROJECT_ROOT / "models" / "Model_seg.m"
MODEL_CLS = PROJECT_ROOT / "models" / "Model_cls.m"
IMG_DIR = PROJECT_ROOT / "test_image" / "最新测试图"
OUT_DIR = Path(__file__).resolve().parent / "outputs" / "batch_latest"
CROPS_DIR = OUT_DIR / "crops"

# Judge 配置（跟现场 config.json judge 一致）
JUDGE = JudgeConfig(
    max_area=10.0,
    sum_area=10.0,
    max_count=10,
    max_length=2.0,
)
NG_TRIGGER = {"隐裂"}


# ─────────── 工具：中文路径读图 ───────────
def imread_cn(path: Path) -> np.ndarray:
    """OpenCV 不认中文路径，用 numpy.fromfile + cv2.imdecode 绕过"""
    arr = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)


def imwrite_cn(path: Path, img: np.ndarray, ext: str = ".png") -> bool:
    """中文路径安全写图"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    buf.tofile(str(path))
    return True


# ─────────── 主流程 ───────────
def main() -> int:
    print("=" * 70)
    print("  批量推理：最新模型 + 最新测试图")
    print(f"  Seg 模型 : {MODEL_SEG}")
    print(f"  Cls 模型 : {MODEL_CLS}")
    print(f"  输入图目录: {IMG_DIR}")
    print(f"  输出目录 : {OUT_DIR}")
    print("=" * 70)

    if not MODEL_SEG.is_file():
        print(f"[FAIL] 找不到 seg 模型: {MODEL_SEG}")
        return 1
    if not MODEL_CLS.is_file():
        print(f"[FAIL] 找不到 cls 模型: {MODEL_CLS}")
        return 1
    if not IMG_DIR.is_dir():
        print(f"[FAIL] 找不到测试图目录: {IMG_DIR}")
        return 1

    bmps = sorted(IMG_DIR.glob("*.bmp"))
    if not bmps:
        print(f"[FAIL] 测试图目录里没有 .bmp")
        return 1
    print(f"\n找到 {len(bmps)} 张测试图")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CROPS_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化 Pipeline（跟现场代码完全相同）
    print("\n初始化 Pipeline ...")
    t0 = time.perf_counter()
    pipeline = Pipeline(
        model_seg_path=MODEL_SEG,
        model_cls_path=MODEL_CLS,
        judge_config=JUDGE,
        ng_trigger_classes=frozenset(NG_TRIGGER),
    )
    print(f"  Pipeline 就绪 ({(time.perf_counter()-t0)*1000:.0f}ms)")

    # 逐张推理
    total_defects = 0
    class_counts: dict[str, int] = {}
    summary = []
    for i, bmp in enumerate(bmps, 1):
        print(f"\n[{i}/{len(bmps)}] {bmp.name}")
        rod_stem = bmp.stem  # 文件名（不含扩展名）

        img = imread_cn(bmp)
        if img is None:
            print(f"  [WARN] 图读取失败，跳过")
            continue
        # 现场 BMP 已经是 1024×3072 uint8（预处理后），Pipeline 会自动判别跳过 preprocess
        # 如果是 uint16 原图（如 15000×1024 TIFF），Pipeline 自动走 preprocess
        print(f"  图: shape={img.shape} dtype={img.dtype}")

        # ★ Pipeline 调用 — 完全跟现场代码一样的链路 ★
        t_proc = time.perf_counter()
        result = pipeline.process(
            img,
            keep_processed_image=True,
            keep_crops=True,         # ★ 保 crop 用于训练 ★
            keep_label_map=True,
            keep_raw_input=False,
        )
        dt_ms = (time.perf_counter() - t_proc) * 1000

        # 结果概要
        n_total = len(result.defects)
        ng_classes_hit = sorted({
            d.class_name for d in result.defects
            if d.class_name in NG_TRIGGER
        })
        print(
            f"  → result={result.result} "
            f"defects={n_total} "
            f"max_area={result.max_area:.0f} sum_area={result.sum_area:.0f} "
            f"max_length={result.max_length:.1f} "
            f"NG类别={ng_classes_hit or '-'} "
            f"({dt_ms:.0f}ms)"
        )

        # ─── 保存整图带框（不染色 — 按你之前要求） ───
        full_marked = draw_marked_full(
            result.processed_image, label_map=None,
            defects=result.defects,
            ng_trigger_classes=NG_TRIGGER,
        )
        out_marked = OUT_DIR / f"{rod_stem}_marked.png"
        if imwrite_cn(out_marked, full_marked):
            print(f"  保存整图: {out_marked.name}")

        # ─── 保存每个缺陷小图（按类别分文件夹，给训练用） ───
        for idx, d in enumerate(result.defects):
            cls = d.class_name or "未分类"
            if d.crop is None:
                continue
            sub_dir = CROPS_DIR / cls
            crop_path = sub_dir / f"{rod_stem}_{idx:02d}_conf{d.class_confidence:.2f}.png"
            if imwrite_cn(crop_path, d.crop):
                class_counts[cls] = class_counts.get(cls, 0) + 1

        total_defects += n_total
        summary.append((
            bmp.name, result.result, n_total, ng_classes_hit, dt_ms
        ))

    # 汇总
    print("\n" + "=" * 70)
    print(f"全部跑完  {len(bmps)} 张 / {total_defects} 个缺陷 / "
          f"{sum(class_counts.values())} 个 crop 已存盘")
    print("\n按类别 crop 数量:")
    for cls, cnt in sorted(class_counts.items(), key=lambda x: -x[1]):
        print(f"  {cls:<10}  {cnt:>4} 张  → tests/outputs/batch_latest/crops/{cls}/")

    print("\n按图结果:")
    print(f"  {'图名':<55}  {'结果':<5} {'缺陷数':<6} {'NG类别':<15} {'耗时'}")
    print("  " + "-" * 100)
    for name, res, n, ng_cls, dt in summary:
        ng_str = ",".join(ng_cls) if ng_cls else "-"
        print(f"  {name:<55}  {res:<5} {n:<6} {ng_str:<15} {dt:.0f}ms")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
