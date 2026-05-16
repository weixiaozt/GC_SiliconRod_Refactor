"""
54 张真实生产图批量回归 / 性能 / 异常巡检
==========================================
对 ``source_image/*.tif``（生产现场原图，uint16 15000×1024）每张：

  - 跑完整 Pipeline (preprocess + seg + cls + judge)
  - 跑 save_inspect_images（5 类图全落盘到 tempdir）
  - 统计耗时、判定结果、缺陷类型分布、抓到的异常

不依赖 PyQt UI、不依赖相机硬件 — 头不动模式，比 demo_ui_mock 快得多。

写盘到 ``tempfile.TemporaryDirectory``，运行完自动清，不会污染 D:/SiRod。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path
from statistics import median

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imread_safe
setup_console_utf8()

import cv2
import numpy as np

from sirod_inspector.algorithm import Pipeline, JudgeConfig
from sirod_inspector.core.tcp_server import InspectData
from main_camera import save_inspect_images


# ── 模型路径（与生产配置一致）──
SEG_MODEL = str(_REPO_ROOT / "models" / "Model_seg.m")
CLS_MODEL = str(_REPO_ROOT / "models" / "Model_cls.m")


def percentile(xs: list, p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> int:
    print("=" * 70)
    print("生产图批量巡检（54 张真实棒图）")
    print("=" * 70)

    src_dir = _REPO_ROOT / "source_image"
    paths = sorted(src_dir.glob("*.tif"))
    print(f"输入: {len(paths)} 张 .tif（按需读盘）")
    print(f"模型: seg={Path(SEG_MODEL).name}, cls={Path(CLS_MODEL).name}")
    print()

    if not paths:
        print("[FAIL] source_image 为空")
        return 1

    # 加载 Pipeline 一次（与生产模式一致）
    print("[1/2] 加载 Pipeline ...", end=" ", flush=True)
    t0 = time.perf_counter()
    pipeline = Pipeline(SEG_MODEL, CLS_MODEL, JudgeConfig())
    print(f"耗时 {time.perf_counter() - t0:.1f}s")
    print()

    # 收集统计
    total_ct = []        # pipeline 总 ms (result.ct_ms)
    save_ct = []         # save_inspect_images ms

    results = Counter()             # OK / NG
    defect_types = Counter()        # 类别 → 次数（NG 触发类）
    cls_dist = Counter()            # 所有被分类的 defect class_name → 次数
    cls_conf_buckets: list = []     # (class_name, confidence, area, length)
    defect_counts = []              # 每张图 defect_count
    exceptions: list = []           # (path, err_msg, traceback)

    # 落盘 tempdir
    with tempfile.TemporaryDirectory(prefix="sirod_scale_") as tmpdir:
        base_dir = Path(tmpdir) / "images"
        raw_tif_dir = Path(tmpdir) / "ImageRaw"
        web_image_dir = Path(tmpdir) / "WebImage"

        print(f"[2/2] 巡检中（落盘到 {tmpdir}）")
        print("-" * 70)

        for i, path in enumerate(paths, 1):
            try:
                # 1) 读图
                img = imread_safe(str(path), cv2.IMREAD_UNCHANGED)
                if img is None:
                    raise RuntimeError(f"读图失败: {path}")

                # rod_id 从文件名提取（格式: 时间戳_棒号.tif）
                stem = path.stem
                rod_id = ""
                if "_" in stem:
                    rod_id = stem.split("_", 1)[1]
                rod_id = rod_id or "NoRead"

                # 2) Pipeline
                result = pipeline.process(
                    img,
                    keep_processed_image=True,
                    keep_crops=True,
                    keep_label_map=True,
                    keep_raw_input=True,
                )
                total_ct.append(float(result.ct_ms))

                results[result.result] += 1
                defect_counts.append(result.defect_count)
                if result.defect_type:
                    defect_types[result.defect_type] += 1

                # 抓每个 defect 的实际分类（修复后所有 defect 都应分类）
                for d in result.defects:
                    name = d.class_name or "(未分类)"
                    cls_dist[name] += 1
                    cls_conf_buckets.append(
                        (name, float(d.class_confidence),
                         int(d.area), float(d.outer_radius))
                    )

                # 3) save_inspect_images（用预处理后的 image，模拟生产）
                data = InspectData(
                    rod_id=rod_id, result=result.result,
                    image=result.processed_image,
                    defect_type=result.defect_type,
                    defect_count=result.defect_count,
                    max_area=float(result.max_area),
                    total_area=float(result.sum_area),
                    max_length=float(result.max_length),
                    raw_json={"defects": []},
                )
                t3 = time.perf_counter()
                paths_out = save_inspect_images(
                    data, result,
                    base_dir=str(base_dir),
                    raw_tif_dir=str(raw_tif_dir),
                    web_image_dir=str(web_image_dir),
                    web_url_base="",
                )
                t4 = time.perf_counter()
                save_ct.append((t4 - t3) * 1000)

                # 验证落盘
                full_raw = paths_out.get("full_raw", "")
                if not full_raw or not Path(full_raw).is_file():
                    raise RuntimeError(f"full_raw 没存出来: {paths_out}")

                # 进度
                if i % 5 == 0 or i == len(paths):
                    mean_ct = sum(total_ct) / len(total_ct)
                    print(f"  [{i:3d}/{len(paths)}] {path.name[:35]:35s} "
                          f"→ {result.result}  type={result.defect_type or '-':6s}  "
                          f"ct={total_ct[-1]:.0f}ms  avg={mean_ct:.0f}ms")
            except Exception as e:
                exceptions.append((path.name, str(e), traceback.format_exc()))
                print(f"  [{i:3d}/{len(paths)}] {path.name[:35]:35s} "
                      f"→ [EXC] {type(e).__name__}: {e}")

        # 计算磁盘占用
        total_bytes = sum(p.stat().st_size for p in Path(tmpdir).rglob("*")
                          if p.is_file())
        file_count = sum(1 for p in Path(tmpdir).rglob("*") if p.is_file())

    # ─── 汇总 ───
    print()
    print("=" * 70)
    print("汇总")
    print("=" * 70)
    n_ok = results.get("OK", 0)
    n_ng = results.get("NG", 0)
    n_exc = len(exceptions)
    n_total = len(paths)
    print(f"总数: {n_total}, OK: {n_ok}, NG: {n_ng}, 异常: {n_exc}")

    if total_ct:
        print(f"\n耗时分布 (ms):")
        for label, xs in [
            ("pipeline", total_ct), ("save_img", save_ct),
        ]:
            if not xs:
                continue
            print(f"  {label}:  "
                  f"min={min(xs):.0f}  "
                  f"p50={median(xs):.0f}  "
                  f"p90={percentile(xs, 90):.0f}  "
                  f"p99={percentile(xs, 99):.0f}  "
                  f"max={max(xs):.0f}  "
                  f"avg={sum(xs)/len(xs):.0f}")

    if defect_types:
        print(f"\nNG 触发类别分布:")
        for t, n in defect_types.most_common():
            print(f"  {t}: {n}")

    if cls_dist:
        print(f"\n所有候选 defect 的分类输出（含未触发 NG 的）:")
        for c, n in cls_dist.most_common():
            print(f"  {c}: {n}")
        print(f"\n候选 defect 详情（class_name, conf, area, radius）:")
        for name, conf, area, rad in cls_conf_buckets:
            print(f"  {name:10s}  conf={conf:.2f}  area={area}  radius={rad:.1f}")

    if defect_counts:
        avg_d = sum(defect_counts) / len(defect_counts)
        print(f"\n每张缺陷数: avg={avg_d:.1f}, max={max(defect_counts)}")

    print(f"\n落盘: {file_count} 个文件, {fmt_bytes(total_bytes)} (已清)")

    if exceptions:
        print(f"\n=== {len(exceptions)} 个异常 ===")
        for name, err, tb in exceptions[:5]:
            print(f"  [{name}]")
            print(f"    {err}")
        if len(exceptions) > 5:
            print(f"  ... 还有 {len(exceptions) - 5} 个未显示")

    print()
    pipeline.close()

    if exceptions:
        print("[FAIL] 有异常")
        return 1
    print("[OK] 全部跑通")
    return 0


if __name__ == "__main__":
    sys.exit(main())
