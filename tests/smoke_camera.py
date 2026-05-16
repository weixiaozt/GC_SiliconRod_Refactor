"""
BV 相机软触发抓图烟雾测试
==========================
1) 枚举所有相机
2) 打开第一台
3) 配置 1024×15000 / SingleFrame / Software trigger
4) 启动 → 软触发 → 拿一帧
5) 把帧落盘到 tests/outputs/camera/ 便于肉眼检查
6) 关闭

用法
----
    python tests/smoke_camera.py
    python tests/smoke_camera.py --width 1024 --height 15000 --exposure 95
    python tests/smoke_camera.py --shots 3       # 连续触发 3 次
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


def main() -> int:
    ap = argparse.ArgumentParser(description="BV 相机软触发烟雾测试")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=15000)
    ap.add_argument("--exposure", type=float, default=None,
                    help="曝光时间 μs（默认沿用相机当前值）")
    ap.add_argument("--shots", type=int, default=1,
                    help="连续触发抓图次数")
    ap.add_argument("--timeout", type=int, default=10000,
                    help="单次抓图超时 ms")
    ap.add_argument("--out-dir", default=str(
        _REPO_ROOT / "tests" / "outputs" / "camera"))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("BV 相机软触发烟雾测试")
    print("=" * 60)

    devs = enumerate_devices()
    if not devs:
        print("[FAIL] 未检测到 BV 相机。检查："
              "(1) 相机供电与网线 "
              "(2) BVCam Viewer 是否未占用 "
              "(3) 防火墙是否放行 GVCP")
        return 1
    print(f"枚举到 {len(devs)} 台设备：")
    for d in devs:
        print(f"  · {d}")
    print()

    try:
        with BVCamera() as cam:
            print(f"已打开: model={cam.model} sn={cam.serial}")

            cam.configure(
                width=args.width, height=args.height,
                trigger_source="Software", trigger_mode="On",
                acquisition_mode="SingleFrame",
                exposure_us=args.exposure,
            )

            # 读回当前实际生效的参数
            try:
                w_eff, h_eff = cam.get_size()
                exp_eff = cam.get_float("ExposureTime", True)
                print(f"实际生效: w={w_eff} h={h_eff} exposure={exp_eff:.1f}us")
            except BVCameraError as e:
                print(f"[WARN] 读取生效参数失败: {e}")

            cam.start()

            for i in range(1, args.shots + 1):
                print(f"\n[{i}/{args.shots}] 软触发...")
                t0 = time.perf_counter()
                frame = cam.trigger_and_grab(timeout_ms=args.timeout)
                dt_ms = (time.perf_counter() - t0) * 1000

                print(f"  返回: shape={frame.shape} dtype={frame.dtype} "
                      f"min={frame.min()} max={frame.max()} "
                      f"mean={frame.mean():.0f}  ({dt_ms:.0f} ms)")

                # 落盘（uint16 保存为 .tif，保留全动态范围）
                ts = time.strftime("%Y%m%d_%H%M%S")
                tif_path = out_dir / f"{ts}_shot{i:02d}.tif"
                ok = imwrite_safe(tif_path, frame, ext=".tif")
                if ok:
                    print(f"  saved tif: {tif_path}")

                # 同时存一个 uint8 预览（动态范围拉伸便于肉眼检查）
                preview = np.clip(
                    (frame.astype(np.float32) - frame.min())
                    / max(1, (frame.max() - frame.min())) * 255.0,
                    0, 255,
                ).astype(np.uint8)
                # 缩到 1/8 方便快速浏览
                if preview.shape[0] > 2000:
                    sh, sw = preview.shape
                    preview = cv2.resize(
                        preview, (sw, sh // 8),
                        interpolation=cv2.INTER_AREA)
                png_path = out_dir / f"{ts}_shot{i:02d}_preview.png"
                imwrite_safe(png_path, preview, ext=".png")
                print(f"  saved preview: {png_path}")

            cam.stop()

        print("\n[OK] 抓图测试完成")
        return 0
    except BVCameraError as e:
        print(f"\n[FAIL] 相机操作异常: {e}")
        return 2
    except Exception as e:
        import traceback
        print(f"\n[FAIL] 未捕获异常: {e}")
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
