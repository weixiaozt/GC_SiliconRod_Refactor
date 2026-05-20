"""
BV 相机软触发抓图烟雾测试
==========================
1) 枚举所有相机
2) 打开第一台
3) 按 --mode 配置 SingleFrame 或 MultiFrame
4) 打印 read_all_params() 让你对比 BV Viewer 里的设置
5) 启动 → 软触发 → 拿一帧（多帧时拼接）
6) 落盘到 tests/outputs/camera/ 便于肉眼检查
7) 关闭

用法
----
    # SingleFrame（老方式，单次扫 15000 行）
    python tests/smoke_camera.py
    python tests/smoke_camera.py --shots 3

    # MultiFrame（跟盐城现场对齐，150 帧 × 100 行 → 15000 行）
    python tests/smoke_camera.py --mode multi
    python tests/smoke_camera.py --mode multi --frame-count 150 --wait-first 5

    # 只打印硬件实际参数，不抓图
    python tests/smoke_camera.py --read-only
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


def _print_params(cam: BVCamera) -> None:
    """打印 read_all_params() 输出 — 让用户对比 BV Viewer 里的设置"""
    try:
        params = cam.read_all_params()
    except Exception as e:
        print(f"[WARN] read_all_params 失败: {e}")
        return

    print("-" * 60)
    print("硬件实时参数（read_all_params）—— 对比 BV Viewer 应该一致：")
    print("-" * 60)
    info_keys = ["model", "serial", "vendor", "ip_addr", "mac_addr"]
    frame_keys = ["width", "height", "acquisition_mode",
                  "acquisition_frame_count"]
    trig_keys = ["trigger_selector", "trigger_mode", "trigger_source",
                 "exposure_us"]
    for label, keys in [("[设备]", info_keys),
                        ("[帧]", frame_keys),
                        ("[触发]", trig_keys)]:
        for k in keys:
            v = params.get(k)
            val = "<读取失败>" if v is None else v
            print(f"  {label:<6} {k:<25} = {val}")
    print("-" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(description="BV 相机软触发烟雾测试")
    ap.add_argument("--mode", choices=("single", "multi"), default="single",
                    help="single = SingleFrame 整张扫一帧 / "
                         "multi = MultiFrame 多帧拼接（跟盐城现场对齐）")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=None,
                    help="single 模式默认 15000；multi 模式默认 100")
    ap.add_argument("--frame-count", type=int, default=150,
                    help="multi 模式：相机一次触发吐多少帧")
    ap.add_argument("--wait-first", type=float, default=5.0,
                    help="multi 模式：首帧后强制等待秒数（对齐 Halcon "
                         "BV_GrapImage:7184 wait_seconds(5)）")
    ap.add_argument("--exposure", type=float, default=None,
                    help="曝光时间 μs（默认沿用相机当前值）")
    ap.add_argument("--shots", type=int, default=1,
                    help="连续触发抓图次数（每次一根棒）")
    ap.add_argument("--timeout", type=int, default=10000,
                    help="单帧 ImageComplete 超时 ms")
    ap.add_argument("--read-only", action="store_true",
                    help="只打印相机硬件参数，不实际抓图（不需要转编码器）")
    ap.add_argument("--out-dir", default=str(
        _REPO_ROOT / "tests" / "outputs" / "camera"))
    args = ap.parse_args()

    # 模式相关的默认 height
    if args.height is None:
        args.height = 100 if args.mode == "multi" else 15000

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("BV 相机软触发烟雾测试")
    if args.mode == "multi":
        total_rows = args.height * args.frame_count
        print(f"模式: MultiFrame  {args.width}×{args.height} × "
              f"{args.frame_count} 帧 = {args.width}×{total_rows} 拼图")
        print(f"      首帧后等待: {args.wait_first}s （对齐 Halcon）")
    else:
        print(f"模式: SingleFrame  {args.width}×{args.height} 整张扫一次")
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
            print(f"        ip={cam.ip_addr} mac={cam.mac_addr} vendor={cam.vendor}")

            # 打开后先读一次"原始硬件值"（用户 BV Viewer 里设的）
            print("\n=== 打开后立刻读硬件（应跟 BV Viewer 一致）===")
            _print_params(cam)

            if args.read_only:
                print("\n[OK] --read-only 模式，跳过 configure 和抓图")
                return 0

            # 应用 CLI 参数
            cfg_kwargs = dict(
                width=args.width, height=args.height,
                trigger_source="Software", trigger_mode="On",
                exposure_us=args.exposure,
            )
            if args.mode == "multi":
                cfg_kwargs["acquisition_mode"] = "MultiFrame"
                cfg_kwargs["acquisition_frame_count"] = args.frame_count
            else:
                cfg_kwargs["acquisition_mode"] = "SingleFrame"
            cam.configure(**cfg_kwargs)

            # configure 后再读一次（验证 set 生效）
            print("\n=== configure 后再读硬件（应跟上面 CLI 参数一致）===")
            _print_params(cam)

            cam.start()

            grab_kwargs = dict(timeout_ms=args.timeout)
            if args.mode == "multi":
                grab_kwargs["frame_count"] = args.frame_count
                grab_kwargs["first_frame_wait_s"] = args.wait_first

            for i in range(1, args.shots + 1):
                print(f"\n[{i}/{args.shots}] 软触发 ...")
                if args.mode == "multi":
                    print(f"   现在转动编码器轮子！需扫够 {args.frame_count} 帧")

                t0 = time.perf_counter()
                try:
                    frame = cam.trigger_and_grab(**grab_kwargs)
                except BVCameraError as e:
                    dt_ms = (time.perf_counter() - t0) * 1000
                    print(f"  [FAIL] 抓图失败 ({dt_ms:.0f} ms): {e}")
                    print(f"  → 通常是编码器没转 / 转得太慢 / 触发没到位")
                    continue
                dt_ms = (time.perf_counter() - t0) * 1000

                print(f"  返回: shape={frame.shape} dtype={frame.dtype} "
                      f"min={frame.min()} max={frame.max()} "
                      f"mean={frame.mean():.0f}  ({dt_ms:.0f} ms)")

                # 多帧模式校验拼接结果尺寸
                if args.mode == "multi":
                    expected = (args.height * args.frame_count, args.width)
                    if frame.shape == expected:
                        print(f"  [OK] 拼接尺寸符合预期: {expected}")
                    else:
                        print(f"  [WARN] 拼接尺寸 {frame.shape} ≠ 期望 {expected}")

                ts = time.strftime("%Y%m%d_%H%M%S")
                tif_path = out_dir / f"{ts}_shot{i:02d}_{args.mode}.tif"
                if imwrite_safe(tif_path, frame, ext=".tif"):
                    print(f"  saved tif: {tif_path}")

                # uint8 预览（动态范围拉伸）
                preview = np.clip(
                    (frame.astype(np.float32) - frame.min())
                    / max(1, (frame.max() - frame.min())) * 255.0,
                    0, 255,
                ).astype(np.uint8)
                if preview.shape[0] > 2000:
                    sh, sw = preview.shape
                    preview = cv2.resize(
                        preview, (sw, sh // 8),
                        interpolation=cv2.INTER_AREA)
                png_path = out_dir / f"{ts}_shot{i:02d}_{args.mode}_preview.png"
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
