"""
单圈 UI 演示 — 跑完 54 张 source_image 自动退出，存图保留供检视
================================================================
跟 demo_ui_mock_camera.py 一样起完整 UI + Mock 相机喂图，但：

  - 第 1 圈结束（54 张全跑完）后自动 quit Qt，不再循环第 2 圈
  - 跑完不清盘 — D:\\SiRod\\ 下所有图保留给用户检视

启动前仍然清一次旧图，让用户看到的只有本次的 54 张输出。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

import demo_ui_mock_camera


def _quit_after_one_lap(lap: int, n_images: int):
    """跑完 1 圈 — 异步通知 UI 线程退出（不阻塞 worker，避免 GIL/Qt mutex 死锁）"""
    print()
    print("=" * 60)
    print(f"[OneLap] 第 {lap} 圈完成（{n_images} 张），通知 UI 退出")
    print(f"[OneLap] 存图保留在 D:\\SiRod\\，结构：")
    print(f"  - D:\\SiRod\\images\\<日期>\\full\\raw\\<OK|NG>\\*.bmp")
    print(f"  - D:\\SiRod\\images\\<日期>\\full\\marked\\<OK|NG>\\*.png")
    print(f"  - D:\\SiRod\\images\\<日期>\\crops\\(raw|marked)\\*  (仅 NG)")
    print(f"  - D:\\SiRod\\ImageRaw\\*.tif")
    print(f"  - D:\\SiRod\\WebImage\\*.png  (仅 NG)")
    print("=" * 60)
    try:
        from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt
        app = QCoreApplication.instance()
        if app is not None:
            QMetaObject.invokeMethod(
                app, "quit", Qt.ConnectionType.QueuedConnection)
    except Exception as e:
        print(f"[OneLap] post quit 异常: {e}")


# 替换 demo 模块的 lap callback（main() 内部按名字查这个对象）
demo_ui_mock_camera._on_lap_complete = _quit_after_one_lap


if __name__ == "__main__":
    print("=" * 60)
    print("单圈 UI 演示 — 跑完 54 张 source_image 自动退出")
    print("存图保留在 D:\\SiRod\\ 供检视")
    print("=" * 60)
    sys.exit(demo_ui_mock_camera.main())
