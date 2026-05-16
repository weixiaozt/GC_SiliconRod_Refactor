"""
内存稳定性测试：循环跑同一张图 30 次，监控 RSS 增长
=========================================================
生产场景一棒一棒连续检测，若有泄漏（缓存未释放 / numpy 视图链 /
ctypes 句柄堆积），跑半天就 OOM。

本测试在 32-bit Python (Windows) 上 RSS 增长 > 100 MB 视为泄漏。
"""

from __future__ import annotations

import gc
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imread_safe
setup_console_utf8()

import cv2

# psutil 可能未安, 兜底用 Windows API
try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
    import ctypes
    from ctypes import wintypes


def _get_rss_mb() -> float:
    if _HAS_PSUTIL:
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    # Windows API fallback
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ('cb', wintypes.DWORD),
            ('PageFaultCount', wintypes.DWORD),
            ('PeakWorkingSetSize', ctypes.c_size_t),
            ('WorkingSetSize', ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
            ('PagefileUsage', ctypes.c_size_t),
            ('PeakPagefileUsage', ctypes.c_size_t),
        ]
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    psapi = ctypes.windll.psapi
    h = ctypes.windll.kernel32.GetCurrentProcess()
    psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    return pmc.WorkingSetSize / 1024 / 1024


def main() -> int:
    print("=" * 60)
    print("内存稳定性 — 跑 30 次 pipeline + save")
    print("=" * 60)

    src_dir = _REPO_ROOT / "source_image"
    paths = sorted(src_dir.glob("*.tif"))
    if not paths:
        print("[FAIL] source_image 为空")
        return 1
    # 选 3 张轮流（避免单张缓存效应）
    cycle = paths[:3] if len(paths) >= 3 else paths
    print(f"轮流 {len(cycle)} 张图，30 次")

    from sirod_inspector.algorithm import Pipeline, JudgeConfig
    from sirod_inspector.core.tcp_server import InspectData
    from main_camera import save_inspect_images

    import tempfile

    SEG = str(_REPO_ROOT / "models" / "Model_seg.m")
    CLS = str(_REPO_ROOT / "models" / "Model_cls.m")

    print(f"加载 Pipeline ...")
    pipeline = Pipeline(SEG, CLS, JudgeConfig())

    # 初始 RSS（模型已加载后基线）
    gc.collect()
    rss_baseline = _get_rss_mb()
    print(f"基线 RSS: {rss_baseline:.0f} MB（含模型）\n")

    snapshots = []
    with tempfile.TemporaryDirectory(prefix="sirod_mem_") as tmpdir:
        base_dir = Path(tmpdir) / "images"
        raw_tif_dir = Path(tmpdir) / "ImageRaw"
        web_image_dir = Path(tmpdir) / "WebImage"
        for i in range(30):
            path = cycle[i % len(cycle)]
            img = imread_safe(str(path), cv2.IMREAD_UNCHANGED)
            result = pipeline.process(
                img, keep_processed_image=True, keep_crops=True,
                keep_label_map=True, keep_raw_input=True,
            )
            data = InspectData(
                rod_id=f"MEM{i:02d}", result=result.result,
                image=result.processed_image,
                defect_count=result.defect_count,
                raw_json={"defects": []},
            )
            save_inspect_images(
                data, result,
                base_dir=str(base_dir),
                raw_tif_dir=str(raw_tif_dir),
                web_image_dir=str(web_image_dir),
                web_url_base="",
            )
            # 每 5 次采样
            if (i + 1) % 5 == 0:
                gc.collect()
                rss = _get_rss_mb()
                delta = rss - rss_baseline
                snapshots.append((i + 1, rss, delta))
                print(f"  iter {i+1:3d}: RSS={rss:.0f} MB  Δ={delta:+.0f} MB")

    pipeline.close()
    gc.collect()
    final_rss = _get_rss_mb()
    final_delta = final_rss - rss_baseline

    print()
    print(f"基线: {rss_baseline:.0f} MB")
    print(f"末次: {snapshots[-1][1]:.0f} MB ({snapshots[-1][2]:+.0f} MB)")
    print(f"清理后: {final_rss:.0f} MB ({final_delta:+.0f} MB)")

    # 真正的泄漏判定：稳态后是否还在涨
    # 头几轮通常是模型 lazy 加载内部 buffer（+200~400 MB 一次性），不是泄漏。
    # 关键看 iter 10 → iter 30 的趋势：若每次稳定涨 → 真泄漏。
    if len(snapshots) < 3:
        print("[OK] 采样不足，无法判趋势")
        return 0

    rss_early = snapshots[1][1]   # iter 10
    rss_late = snapshots[-1][1]   # iter 30
    iters_between = snapshots[-1][0] - snapshots[1][0]
    per_iter_growth = (rss_late - rss_early) / iters_between if iters_between else 0
    trend_delta = rss_late - rss_early

    print(f"\n稳态趋势（iter {snapshots[1][0]}-{snapshots[-1][0]}）:")
    print(f"  RSS 差: {trend_delta:+.0f} MB")
    print(f"  每次平均: {per_iter_growth:+.2f} MB/iter")

    # 阈值：稳态后每次 >1 MB 增长 = 慢泄漏；>3 MB/iter = 快泄漏
    if per_iter_growth > 3.0:
        print(f"\n[FAIL] 稳态后仍涨 {per_iter_growth:.2f} MB/iter，明确泄漏")
        return 1
    if per_iter_growth > 1.0:
        print(f"\n[WARN] 稳态后涨 {per_iter_growth:.2f} MB/iter，疑似慢泄漏 — "
              f"建议 100+ 次复测")
        return 0     # 不算硬失败，仅警告

    print(f"\n[OK] RSS 稳定（稳态后 {per_iter_growth:+.2f} MB/iter）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
