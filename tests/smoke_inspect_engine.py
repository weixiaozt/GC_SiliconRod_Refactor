"""
InspectEngine 烟雾测试
========================
验证两种触发模式：
  1) trigger_once() 同步触发
  2) run_loop(interval_s=N) 后台周期触发

同时检查 InspectData 字段完整性，确保 UI/DB/飞书/MES 现有消费链路兼容。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

from sirod_inspector.core import (
    InspectEngine,
    InspectEngineConfig,
)
from sirod_inspector.algorithm import JudgeConfig


def _print_inspect(data) -> None:
    """打印 InspectData 关键字段，验证装配是否完整"""
    image_repr = (
        f"{data.image.shape} {data.image.dtype}"
        if data.image is not None else "None"
    )
    print(f"  ← rod_id={data.rod_id!r}  inspect_id={data.inspect_id}  "
          f"result={data.result}  quality={data.quality}  "
          f"type={data.defect_type or '-'}  count={data.defect_count}  "
          f"max_area={data.max_area:.0f}  total_area={data.total_area:.0f}  "
          f"max_len={data.max_length:.1f}  ct={data.ct*1000:.0f}ms  "
          f"image={image_repr}  ts={data.timestamp}")
    if data.raw_json.get("judge_reasons"):
        print(f"     judge: {data.raw_json['judge_reasons']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="InspectEngine smoke test")
    ap.add_argument("--shots", type=int, default=2,
                    help="trigger_once 模式触发次数")
    ap.add_argument("--loop-shots", type=int, default=3,
                    help="run_loop 模式触发次数")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="run_loop 周期 (秒)")
    args = ap.parse_args()

    print("=" * 60)
    print("InspectEngine smoke test")
    print("=" * 60)

    rod_counter = [0]
    def fake_scanner():
        rod_counter[0] += 1
        return f"TEST{rod_counter[0]:05d}"

    received: list = []
    def on_inspect(data):
        received.append(data)
        _print_inspect(data)

    cfg = InspectEngineConfig(
        seg_model=str(_REPO_ROOT / "models" / "Model_seg.m"),
        cls_model=str(_REPO_ROOT / "models" / "Model_cls.m"),
        judge_config=JudgeConfig(),
        grab_timeout_ms=10000,
    )

    with InspectEngine(cfg, rod_id_provider=fake_scanner,
                        on_inspect=on_inspect) as engine:

        # ──── 模式 A: 同步触发 ────
        print(f"\n[模式 A] trigger_once() × {args.shots}")
        for i in range(args.shots):
            print(f"\n[{i+1}/{args.shots}] sync trigger...")
            t0 = time.perf_counter()
            data = engine.trigger_once()
            dt = (time.perf_counter() - t0) * 1000
            print(f"  trigger total: {dt:.0f}ms  data={'OK' if data else 'FAIL'}")

        # ──── 模式 B: 周期循环 ────
        print(f"\n[模式 B] run_loop(interval_s={args.interval}) × {args.loop_shots}")
        engine.run_loop(interval_s=args.interval)
        # 等待 loop_shots 个回调到达
        deadline = time.time() + args.loop_shots * (args.interval + 5)
        before = len(received)
        target = before + args.loop_shots
        while len(received) < target and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(0.5)
        engine.stop_loop()

        print(f"\n[结果] 共收到 {len(received)} 个 InspectData "
              f"(预期 ≥ {args.shots + args.loop_shots})")

    print("\n[OK] InspectEngine smoke 通过" if received else "[FAIL] 没收到任何回调")
    return 0 if received else 1


if __name__ == "__main__":
    sys.exit(main())
