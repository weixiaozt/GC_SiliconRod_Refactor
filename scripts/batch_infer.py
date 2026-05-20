"""
通用批量推理脚本
==================
不依赖 PyQt UI，无侵入跑现场算法链路（Pipeline = seg + cls + judge）。

交互流程：
  1. 弹文件选择框选 seg 模型 (.m)
  2. 弹文件选择框选 cls 模型 (.m)
  3. 弹目录选择框选源图文件夹
  4. 弹目录选择框选输出根目录
  5. 自动遍历源图目录推理，每张图：
       - 整图带框 / 类别名      → <输出>/marked/<原文件名>.png
       - 每个缺陷小图            → <输出>/crops/<类别>/<原文件名>_<序号>_conf<置信度>.png
       - 推理结果 CSV 汇总        → <输出>/result.csv

支持的图格式：.bmp / .png / .jpg / .jpeg / .tif / .tiff （uint8 1024×3072 跳预处理；
其他尺寸 uint16 自动走 preprocess）

用法::

    # 交互模式（弹 4 个对话框）
    python scripts/batch_infer.py

    # CLI 跳过对话框（适合脚本批跑）
    python scripts/batch_infer.py --seg D:/path/Model_seg.m --cls D:/path/Model_cls.m \\
                                  --input D:/path/imgs --output D:/path/out
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Optional

# 项目根目录加入 sys.path 让我们能 import sirod_inspector
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# ─────────── Windows 控制台 UTF-8 ───────────
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import cv2
import numpy as np

# 延迟 import，让上面的 sys.path 生效
from sirod_inspector.algorithm import JudgeConfig, Pipeline
from sirod_inspector.algorithm.overlay import draw_marked_full


SUPPORTED_EXT = {".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


# ─────────── 中文路径安全读写图 ───────────
def imread_safe(path: Path) -> Optional[np.ndarray]:
    """OpenCV 不认中文路径 → 用 numpy.fromfile + cv2.imdecode 绕过"""
    try:
        arr = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    except Exception as e:
        print(f"  [WARN] 读图失败 {path}: {e}")
        return None


def imwrite_safe(path: Path, img: np.ndarray, ext: str = ".png") -> bool:
    """中文路径安全写图"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(ext, img)
        if not ok:
            return False
        buf.tofile(str(path))
        return True
    except Exception as e:
        print(f"  [WARN] 写图失败 {path}: {e}")
        return False


# ─────────── tkinter 对话框选文件/目录 ───────────
def _pick_file(title: str, filetypes: list[tuple[str, str]],
               initialdir: Optional[str] = None) -> Optional[Path]:
    """弹文件选择框。返回 Path 或 None（用户取消）"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print(f"[FAIL] tkinter 不可用，请用 --seg / --cls 等 CLI 参数指定")
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    fname = filedialog.askopenfilename(
        title=title, filetypes=filetypes,
        initialdir=initialdir,
    )
    root.destroy()
    return Path(fname) if fname else None


def _pick_dir(title: str,
              initialdir: Optional[str] = None) -> Optional[Path]:
    """弹目录选择框。返回 Path 或 None"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print(f"[FAIL] tkinter 不可用，请用 --input / --output 等 CLI 参数指定")
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    d = filedialog.askdirectory(
        title=title, initialdir=initialdir, mustexist=True,
    )
    root.destroy()
    return Path(d) if d else None


# ─────────── 主流程 ───────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="批量推理（seg → cls → judge，跟现场同一份算法代码）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--seg", type=Path, help="seg 模型路径 (.m)；不传则弹对话框")
    ap.add_argument("--cls", type=Path, help="cls 模型路径 (.m)；不传则弹对话框")
    ap.add_argument("--input", type=Path,
                    help="源图目录；不传则弹对话框")
    ap.add_argument("--output", type=Path,
                    help="输出根目录；不传则弹对话框")
    ap.add_argument("--max-area", type=float, default=10.0,
                    help="judge 最大面积阈值（默认 10）")
    ap.add_argument("--max-length", type=float, default=2.0,
                    help="judge 最大长度阈值（默认 2）")
    ap.add_argument("--ng-trigger", type=str, default="隐裂",
                    help="触发 NG 的类别（逗号分隔，默认 '隐裂'）")
    args = ap.parse_args()

    print("=" * 70)
    print("  通用批量推理")
    print("=" * 70)

    # ─── 1) seg 模型 ───
    seg_path = args.seg
    if seg_path is None:
        print("\n[1/4] 选择 Seg 模型 (.m)")
        seg_path = _pick_file(
            "选择 Seg 模型 (.m)",
            [("模型文件", "*.m"), ("所有文件", "*.*")],
        )
        if seg_path is None:
            print("  [取消]")
            return 1
    print(f"  ✓ Seg: {seg_path}")
    if not seg_path.is_file():
        print(f"  [FAIL] 文件不存在")
        return 1

    # ─── 2) cls 模型 ───
    cls_path = args.cls
    if cls_path is None:
        print("\n[2/4] 选择 Cls 模型 (.m)")
        cls_path = _pick_file(
            "选择 Cls 模型 (.m)",
            [("模型文件", "*.m"), ("所有文件", "*.*")],
            initialdir=str(seg_path.parent),
        )
        if cls_path is None:
            print("  [取消]")
            return 1
    print(f"  ✓ Cls: {cls_path}")
    if not cls_path.is_file():
        print(f"  [FAIL] 文件不存在")
        return 1

    # ─── 3) 源图目录 ───
    input_dir = args.input
    if input_dir is None:
        print("\n[3/4] 选择源图目录")
        input_dir = _pick_dir("选择源图目录")
        if input_dir is None:
            print("  [取消]")
            return 1
    print(f"  ✓ 源图: {input_dir}")
    if not input_dir.is_dir():
        print(f"  [FAIL] 目录不存在")
        return 1

    # 扫描图片
    images = [p for p in sorted(input_dir.rglob("*"))
              if p.is_file() and p.suffix.lower() in SUPPORTED_EXT]
    if not images:
        print(f"  [FAIL] 目录下没有支持的图片"
              f" ({', '.join(sorted(SUPPORTED_EXT))})")
        return 1
    print(f"    扫到 {len(images)} 张图")

    # ─── 4) 输出根目录 ───
    output_dir = args.output
    if output_dir is None:
        print("\n[4/4] 选择输出根目录")
        output_dir = _pick_dir(
            "选择输出根目录（marked / crops / result.csv 会写到这里）",
            initialdir=str(input_dir.parent),
        )
        if output_dir is None:
            print("  [取消]")
            return 1
    print(f"  ✓ 输出: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    marked_dir = output_dir / "marked"
    crops_dir = output_dir / "crops"
    marked_dir.mkdir(exist_ok=True)
    crops_dir.mkdir(exist_ok=True)

    # ─── 初始化 Pipeline ───
    print("\n" + "=" * 70)
    print("  初始化 Pipeline ...")
    ng_trigger = frozenset(
        s.strip() for s in args.ng_trigger.split(",") if s.strip()
    )
    judge_cfg = JudgeConfig(
        max_area=args.max_area,
        sum_area=args.max_area,
        max_count=10,
        max_length=args.max_length,
    )
    t_init = time.perf_counter()
    pipeline = Pipeline(
        model_seg_path=seg_path,
        model_cls_path=cls_path,
        judge_config=judge_cfg,
        ng_trigger_classes=ng_trigger,
    )
    print(f"  Pipeline 就绪 ({(time.perf_counter()-t_init)*1000:.0f}ms)")
    print(f"  Judge: max_area={args.max_area} max_length={args.max_length} "
          f"ng_trigger={sorted(ng_trigger)}")
    print("=" * 70)

    # ─── 逐张推理 ───
    class_counts: dict[str, int] = {}
    summary = []     # 每张图：(name, result, n_defects, ng_classes, ct_ms)
    total_defects = 0
    csv_rows = [
        ("文件名", "结果", "缺陷数", "NG类别", "max_area",
         "sum_area", "max_length", "耗时ms"),
    ]
    t_start_total = time.perf_counter()

    for i, img_path in enumerate(images, 1):
        rel = img_path.relative_to(input_dir)
        rod_stem = img_path.stem
        print(f"\n[{i}/{len(images)}] {rel}")

        img = imread_safe(img_path)
        if img is None:
            print(f"  [跳过] 读图失败")
            continue
        print(f"  shape={img.shape} dtype={img.dtype}")

        # ★ Pipeline.process — 跟现场代码一字不差的 seg → cls → judge ★
        t_proc = time.perf_counter()
        try:
            result = pipeline.process(
                img,
                keep_processed_image=True,
                keep_crops=True,
                keep_label_map=True,
                keep_raw_input=False,
            )
        except Exception as e:
            print(f"  [FAIL] 推理异常: {e}")
            continue
        dt_ms = (time.perf_counter() - t_proc) * 1000

        n = len(result.defects)
        ng_hit = sorted({
            d.class_name for d in result.defects
            if d.class_name in ng_trigger
        })
        ng_str = ",".join(ng_hit) if ng_hit else "-"
        print(
            f"  → result={result.result} defects={n} "
            f"max_area={result.max_area:.0f} max_length={result.max_length:.1f} "
            f"NG类别={ng_str} ({dt_ms:.0f}ms)"
        )

        # ─── 保存整图带框（不染色，按现场要求） ───
        marked = draw_marked_full(
            result.processed_image, label_map=None,
            defects=result.defects,
            ng_trigger_classes=ng_trigger,
        )
        out_marked = marked_dir / f"{rod_stem}.png"
        if imwrite_safe(out_marked, marked):
            print(f"  保存整图: marked/{out_marked.name}")

        # ─── 保存每个缺陷小图（按类分文件夹） ───
        for idx, d in enumerate(result.defects):
            cls = d.class_name or "未分类"
            if d.crop is None:
                continue
            sub = crops_dir / cls
            crop_path = sub / f"{rod_stem}_{idx:02d}_conf{d.class_confidence:.2f}.png"
            if imwrite_safe(crop_path, d.crop):
                class_counts[cls] = class_counts.get(cls, 0) + 1

        total_defects += n
        summary.append((str(rel), result.result, n, ng_str, dt_ms))
        csv_rows.append((
            str(rel), result.result, n, ng_str,
            f"{result.max_area:.0f}", f"{result.sum_area:.0f}",
            f"{result.max_length:.1f}", f"{dt_ms:.0f}",
        ))

    # ─── 写汇总 CSV ───
    csv_path = output_dir / "result.csv"
    try:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(csv_rows)
        print(f"\n✓ 结果 CSV: {csv_path}")
    except Exception as e:
        print(f"\n[WARN] 写 CSV 失败: {e}")

    # ─── 汇总 ───
    total_s = time.perf_counter() - t_start_total
    print("\n" + "=" * 70)
    print(f"  全部跑完  {len(images)} 张 / {total_defects} 个缺陷 / "
          f"{sum(class_counts.values())} 个 crop")
    print(f"  总耗时 {total_s:.1f}s "
          f"(平均 {total_s/max(1,len(images))*1000:.0f}ms / 张)")
    print(f"\n  按类别 crop 数量:")
    for cls, cnt in sorted(class_counts.items(), key=lambda x: -x[1]):
        print(f"    {cls:<12}  {cnt:>4} 张  → {crops_dir / cls}")

    print(f"\n  输出根目录: {output_dir}")
    print(f"    ├── marked/       整图带框 + 类别名（{len(images)} 张）")
    print(f"    ├── crops/<类别>/ 缺陷小图（按类分文件夹）")
    print(f"    └── result.csv    每张图判定 + 缺陷统计")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
