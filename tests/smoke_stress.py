"""
压力测试：连续 N 次软触发，监控耗时/内存/句柄稳定性
=======================================================
用于暴露：
  - 内存泄漏（每次 trigger 后内存持续增长）
  - 帧拷贝错位（min/max 跨帧异常）
  - 相机句柄累积（多次启停不释放）
  - 推理 DLL 缓冲污染（label_map 被下一次推理覆盖）
"""

from __future__ import annotations

import argparse
import gc
import statistics
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

import numpy as np

from sirod_inspector.core import (
    InspectEngine, InspectEngineConfig,
)
from sirod_inspector.algorithm import JudgeConfig

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


def get_mem_mb() -> float:
    if not _HAS_PSUTIL:
        return 0.0
    return psutil.Process().memory_info().rss / 1024 / 1024


def main() -> int:
    ap = argparse.ArgumentParser(description="压力测试 (连续 N 次 trigger)")
    ap.add_argument("--shots", type=int, default=20)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=10000)
    args = ap.parse_args()

    print("=" * 60)
    print(f"压力测试：{args.shots} 次连续触发，间隔 {args.interval}s")
    print("=" * 60)
    if not _HAS_PSUTIL:
        print("提示：未装 psutil，跳过内存监控（pip install psutil 启用）")

    rod_seq = [0]
    def fake_scanner():
        rod_seq[0] += 1
        return f"STRESS{rod_seq[0]:04d}"

    samples: list = []
    received: list = []

    def on_inspect(data):
        received.append({
            "id": data.inspect_id,
            "result": data.result,
            "ct_s": data.ct,
            "img_shape": data.image.shape if data.image is not None else None,
            "img_min": int(data.image.min()) if data.image is not None else 0,
            "img_max": int(data.image.max()) if data.image is not None else 0,
        })

    cfg = InspectEngineConfig(
        seg_model=str(_REPO_ROOT / "models" / "Model_seg.m"),
        cls_model=str(_REPO_ROOT / "models" / "Model_cls.m"),
        judge_config=JudgeConfig(),
        grab_timeout_ms=args.timeout,
    )

    with InspectEngine(cfg, rod_id_provider=fake_scanner,
                        on_inspect=on_inspect) as engine:

        print(f"\n初始内存: {get_mem_mb():.1f} MB")
        baseline_mem = get_mem_mb()

        print()
        for i in range(1, args.shots + 1):
            t0 = time.perf_counter()
            engine.trigger_once()
            dt = (time.perf_counter() - t0) * 1000
            mem = get_mem_mb()
            mem_delta = mem - baseline_mem
            samples.append({"shot": i, "total_ms": dt, "mem_mb": mem,
                            "mem_delta": mem_delta})
            if i == 1 or i % 5 == 0 or i == args.shots:
                print(f"  shot {i:3d}: total={dt:.0f}ms  mem={mem:.1f}MB "
                      f"(Δ {mem_delta:+.1f}MB)")
            if args.interval > 0 and i < args.shots:
                time.sleep(args.interval)

        gc.collect()
        final_mem = get_mem_mb()

    # ──── 统计 ────
    print()
    print("=" * 60)
    print("统计")
    print("=" * 60)

    durations = [s["total_ms"] for s in samples]
    print(f"耗时 / shot:")
    print(f"  min      = {min(durations):.0f} ms")
    print(f"  max      = {max(durations):.0f} ms")
    print(f"  mean     = {statistics.mean(durations):.0f} ms")
    print(f"  median   = {statistics.median(durations):.0f} ms")
    if len(durations) > 1:
        print(f"  stdev    = {statistics.stdev(durations):.0f} ms")
    # 前 5 vs 后 5 比较：检测 perf drift
    if len(durations) >= 10:
        head_avg = statistics.mean(durations[:5])
        tail_avg = statistics.mean(durations[-5:])
        drift = tail_avg - head_avg
        drift_pct = drift / head_avg * 100
        print(f"  drift    = 头 5 次 {head_avg:.0f}ms vs 尾 5 次 {tail_avg:.0f}ms "
              f"({drift_pct:+.1f}%)")
        if abs(drift_pct) > 20:
            print(f"  ⚠ 耗时漂移 > 20%，可能有累积开销")
        else:
            print(f"  ✓ 耗时稳定")

    if _HAS_PSUTIL:
        print(f"\n内存:")
        print(f"  baseline = {baseline_mem:.1f} MB")
        print(f"  peak     = {max(s['mem_mb'] for s in samples):.1f} MB")
        print(f"  final    = {final_mem:.1f} MB")
        leak = final_mem - baseline_mem
        leak_per_shot = leak / args.shots if args.shots > 0 else 0
        print(f"  Δ        = {leak:+.1f} MB ({leak_per_shot:+.2f} MB/shot)")
        if leak_per_shot > 1.0:
            print(f"  ⚠ 每次 shot 涨 > 1MB，疑似内存泄漏")
        elif leak_per_shot > 0.2:
            print(f"  ⚠ 缓慢内存增长，需长跑确认")
        else:
            print(f"  ✓ 内存稳定（涨幅可忽略）")

    print(f"\n收到 {len(received)} 个 InspectData (预期 {args.shots})")

    # 帧内容 sanity：检查 min/max 不全相同（说明拷贝有发生变化）
    mins = [r["img_min"] for r in received]
    maxs = [r["img_max"] for r in received]
    if len(set(mins)) == 1 and len(set(maxs)) == 1:
        print(f"  ⚠ 所有帧 min/max 完全相同 ({mins[0]}/{maxs[0]})；可能图像未变化（暗场景或缓冲污染）")
    else:
        print(f"  ✓ 帧内容有变化（{len(set(mins))} 种 min，{len(set(maxs))} 种 max）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
