"""
端到端实拍测试：相机 → 预处理 → 检测流水线
============================================
从 BV 相机软触发抓一帧 → preprocess → Pipeline 全链路跑一次。

这是工厂模式上线后会跑的真实链路，仅缺：
  - 扫码枪输入棒号
  - 报警串口输出 NG
  - 数据库 / 飞书 / MES 上传
这些在 UI 集成阶段接入。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imwrite_safe
setup_console_utf8()

import cv2
import numpy as np

from sirod_inspector.camera import BVCamera, enumerate_devices, BVCameraError
from sirod_inspector.algorithm import Pipeline, JudgeConfig


_DEFAULT_SEG = _REPO_ROOT / "models" / "Model_seg.m"
_DEFAULT_CLS = _REPO_ROOT / "models" / "Model_cls.m"
_DEFAULT_OUT = _REPO_ROOT / "tests" / "outputs" / "live_pipeline"


def main() -> int:
    ap = argparse.ArgumentParser(description="实拍端到端流水线测试")
    ap.add_argument("--shots", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=10000)
    ap.add_argument("--exposure", type=float, default=None)
    ap.add_argument("--seg", default=str(_DEFAULT_SEG))
    ap.add_argument("--cls", default=str(_DEFAULT_CLS))
    ap.add_argument("--out-dir", default=str(_DEFAULT_OUT))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("端到端实拍流水线")
    print("=" * 60)
    devs = enumerate_devices()
    if not devs:
        print("[FAIL] 未检测到相机")
        return 1
    print(f"相机: {devs[0]}")

    with BVCamera() as cam, Pipeline(args.seg, args.cls, JudgeConfig()) as pipe:
        cam.configure(width=1024, height=15000,
                       trigger_source="Software", trigger_mode="On",
                       acquisition_mode="SingleFrame",
                       exposure_us=args.exposure)
        cam.start()

        for i in range(1, args.shots + 1):
            print(f"\n[{i}/{args.shots}] 触发 → 抓图 → 检测")
            t0 = time.perf_counter()
            frame = cam.trigger_and_grab(timeout_ms=args.timeout)
            t_grab = (time.perf_counter() - t0) * 1000

            result = pipe.process(frame, keep_crops=True)
            t_total = (time.perf_counter() - t0) * 1000

            print(f"  grab={t_grab:.0f}ms  inspect={result.ct_ms:.0f}ms  "
                  f"total={t_total:.0f}ms")
            print(f"  result={result.result} type={result.defect_type or '-'} "
                  f"count={result.defect_count} "
                  f"max_area={result.max_area:.0f} "
                  f"max_len={result.max_length:.1f}")
            if result.judge_reasons:
                print(f"  judge: {'; '.join(result.judge_reasons)}")
            for j, d in enumerate(result.defects):
                if d.class_name:
                    print(f"    [{j}] {d.class_name} "
                          f"({d.class_confidence:.3f}) bbox={d.bbox} "
                          f"a={d.area} r={d.outer_radius:.1f}")

            # 存盘
            ts = time.strftime("%Y%m%d_%H%M%S")
            stem = f"{ts}_shot{i:02d}_{result.result}"
            # 原图（uint16, .tif）
            imwrite_safe(out_dir / f"{stem}_raw.tif", frame, ext=".tif")
            # 预处理后图
            if result.processed_image is not None:
                imwrite_safe(out_dir / f"{stem}_processed.bmp",
                             result.processed_image, ext=".bmp")

        cam.stop()

    print(f"\n[OK] 完成。产物在 {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
