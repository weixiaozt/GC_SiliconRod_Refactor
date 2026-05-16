"""
main_camera.py 集成烟雾测试（headless）
========================================
在 offscreen Qt 平台下启动 SiRodCameraApp：
  1) 初始化（DB / 飞书 / 串口 / 引擎 / UI 全部就位）
  2) 触发 N 次检测
  3) 验证 UI 消费链路（OverviewPage / Gallery）能收到 InspectData
  4) 优雅关闭

不会显示窗口，适合 CI / 自动回归。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 必须在 import PyQt6 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer


def main() -> int:
    ap = argparse.ArgumentParser(
        description="main_camera headless 集成测试")
    ap.add_argument("--shots", type=int, default=2,
                    help="触发抓图次数")
    ap.add_argument("--shot-interval-ms", type=int, default=3500,
                    help="触发间隔 ms（含相机+推理）")
    args = ap.parse_args()

    print("=" * 60)
    print("main_camera.py headless 集成测试")
    print("=" * 60)

    app = QApplication(sys.argv)

    # 延迟 import 以让 setup_console_utf8 / QT_QPA_PLATFORM 生效
    from main_camera import SiRodCameraApp

    controller = SiRodCameraApp()

    # 校验关键属性都已就位
    checks = [
        ("config",          hasattr(controller, "config")),
        ("database",        hasattr(controller, "database")),
        ("feishu",          hasattr(controller, "feishu")),
        ("serial_manager",  hasattr(controller, "serial_manager")),
        ("http_client",     hasattr(controller, "http_client")),
        ("engine",          hasattr(controller, "engine")),
        ("overview_page",   hasattr(controller, "overview_page")),
        ("history_page",    hasattr(controller, "history_page")),
        ("gallery_page",    hasattr(controller, "gallery_page")),
        ("stats_page",      hasattr(controller, "stats_page")),
        ("settings_page",   hasattr(controller, "settings_page")),
        ("window",          hasattr(controller, "window")),
        ("inspect signal",  hasattr(controller, "_inspect_data_signal")),
    ]
    print("\n[init 检查]")
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    if not all(ok for _, ok in checks):
        return 1

    # 用受控方式启动（不调 controller.start() 避免 showMaximized 显示窗口）
    print("\n[启动子服务]")
    try:
        controller.engine.start()
        print(f"  ✓ 引擎: model={controller.engine._camera.model}")
    except Exception as e:
        print(f"  ✗ 引擎启动失败: {e}")
        return 2

    # 设置 rod_id 注入器（mock 扫码枪）
    rod_seq = [0]
    def fake_rod():
        rod_seq[0] += 1
        return f"HEADLESS_{rod_seq[0]:04d}"
    # 重新绑定 provider
    controller._latest_rod_id = "HEADLESS_INIT"
    controller.engine.rod_id_provider = fake_rod

    # 记录收到的 InspectData（覆盖 overview_page 的 on_inspect_data 钩子）
    received: list = []
    original_handler = controller._handle_inspect_data

    def trace_handler(data):
        received.append(data)
        # 跳过弹窗（headless 下 QMessageBox 会卡）— 临时禁用 alarm
        try:
            if hasattr(controller.overview_page, "_alarm_checkbox"):
                controller.overview_page._alarm_checkbox.setChecked(False)
        except Exception:
            pass
        try:
            original_handler(data)
        except Exception as e:
            print(f"  [WARN] handler 异常: {e}")

    # 重新连接信号
    try:
        controller._inspect_data_signal.disconnect()
    except TypeError:
        pass
    controller._inspect_data_signal.connect(trace_handler)

    # 触发
    print(f"\n[触发 {args.shots} 次]")
    shot_count = [0]

    def fire_one():
        if shot_count[0] >= args.shots:
            QTimer.singleShot(2000, app.quit)
            return
        shot_count[0] += 1
        print(f"  shot {shot_count[0]}: trigger...")
        try:
            controller.engine.trigger_once()
        except Exception as e:
            print(f"  [WARN] trigger 异常: {e}")
        QTimer.singleShot(args.shot_interval_ms, fire_one)

    QTimer.singleShot(500, fire_one)

    rc = app.exec()

    # 关闭
    print("\n[关闭]")
    try:
        controller.engine.stop()
        print("  ✓ 引擎已关闭")
    except Exception as e:
        print(f"  ✗ 引擎关闭异常: {e}")

    print(f"\n[结果] received {len(received)} 个 InspectData "
          f"(预期 {args.shots})")
    if received:
        d = received[-1]
        image_repr = (
            f"{d.image.shape} {d.image.dtype}" if d.image is not None else "None"
        )
        print(f"  最后一帧: rod_id={d.rod_id!r}  inspect_id={d.inspect_id}  "
              f"result={d.result}  image={image_repr}")

    ok_count = len(received) >= args.shots
    overview_count = getattr(controller.overview_page, "_total", 0)
    print(f"  OverviewPage._total = {overview_count} (应 ≥ {args.shots})")
    ok_overview = overview_count >= args.shots

    final = ok_count and ok_overview
    print("\n[OK] 集成测试通过" if final else "[FAIL] 集成测试未达预期")
    return 0 if final else 3


if __name__ == "__main__":
    sys.exit(main())
