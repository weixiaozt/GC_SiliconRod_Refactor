"""
test_image 全集 UI 演示 — 跑完所有 .bmp 自动退出，存图保留供训练
================================================================
跟 demo_ui_one_lap.py 类似，但数据源换成 ``test_image/*.bmp``：
  - 412 张预处理后 uint8 1024×3072（已经是 AI 模型输入尺寸）
  - Pipeline 检测到 uint8 自动跳预处理，直接送 seg → cls
  - 跑完 1 圈自动退出
  - **不**清盘（用户要拿存图做训练数据）

启动前清一次旧的 D:\\SiRod，让本次 412 张输出干净。
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

import threading
import time
import cv2
import numpy as np

from _test_utils import imread_safe

# 加载原 demo 模块（要用 _cleanup_sirod_disk + MockBVCamera 类）
import demo_ui_mock_camera


# ──────────────────────────────────────────────────────────────
# 重写 MockBVCamera：从 test_image 读 .bmp，跳过 .zip
# ──────────────────────────────────────────────────────────────

_TEST_IMAGE_DIR = _REPO_ROOT / "test_image"


def _new_init(self, uid: int = 0, **kwargs):
    self.model = "Mock-BV-C3110GE"
    self.serial = "MOCK001"
    self._streaming = False
    # 只挑 .bmp（.zip 是训练图压缩包，.jpg 不是棒图）
    import os
    limit = int(os.environ.get("DEMO_IMAGE_LIMIT", "0"))  # 0 = 全部
    all_paths = sorted(_TEST_IMAGE_DIR.glob("*.bmp"))
    self._image_paths = all_paths[:limit] if limit > 0 else all_paths
    if not self._image_paths:
        raise RuntimeError(f"未找到 test_image/*.bmp，路径: {_TEST_IMAGE_DIR}")
    self._idx = 0
    self._lap = 0
    self._lock = threading.Lock()
    print(f"[MockBVCamera] 数据源: {len(self._image_paths)} 张 .bmp "
          f"(uint8 预处理后图，pipeline 跳预处理直接送 seg)")
    for i, p in enumerate(self._image_paths[:5]):
        print(f"  [{i}] {p.name}")
    if len(self._image_paths) > 5:
        print(f"  ... 共 {len(self._image_paths)} 张")


def _new_trigger_and_grab(self, timeout_ms: int = 5000):
    """跟原版一样但延迟从 0.8s → 0.2s（uint8 不模拟线扫耗时）"""
    if not self._streaming:
        raise RuntimeError("MockBVCamera 未启动")
    time.sleep(0.2)
    with self._lock:
        path = self._image_paths[self._idx]
        next_idx = self._idx + 1
        lap_done = next_idx >= len(self._image_paths)
        self._idx = 0 if lap_done else next_idx
        if lap_done:
            self._lap += 1
    img = imread_safe(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"读图失败: {path}")
    print(f"[MockBVCamera] 触发 → 返回 {path.name}"
          + (f"  [圈 {self._lap} 已完成]" if lap_done else ""))
    if lap_done and demo_ui_mock_camera.MockBVCamera.on_lap_complete is not None:
        try:
            demo_ui_mock_camera.MockBVCamera.on_lap_complete(
                self._lap, len(self._image_paths))
        except Exception as e:
            print(f"[MockBVCamera] on_lap_complete 异常: {e}")
    return img


def _quit_after_one_lap(lap: int, n_images: int):
    """跑完 1 圈 — 异步通知 UI 线程退出 Qt（不阻塞 worker 线程）。

    ★ 不能在 worker 线程直接调 QCoreApplication.quit()，会跟 UI 线程
    死锁：worker 持 GIL 调 Qt C++，Qt C++ 要等 UI 线程的 event queue
    mutex，UI 线程要等 GIL 才能处理 Python 信号槽 → 循环等待。
    实测：worker iter=10 callback 调 quit() 后 worker 永远不出来，
    UI 心跳也停了，整个进程僵死。

    改成 QMetaObject.invokeMethod 异步 post 一个 "quit" 调用到 UI 线程
    队列，worker 不等。UI 线程在下一次事件循环 iter 里取走执行。
    """
    print()
    print("=" * 60)
    print(f"[OneLap] 第 {lap} 圈完成（{n_images} 张），通知 UI 退出")
    print(f"[OneLap] 存图保留在 D:\\SiRod\\，供训练用：")
    print(f"  - crops/raw/   小图原图（训练用）")
    print(f"  - crops/marked/ 小图标注（看效果）")
    print(f"  - full/raw/    大图原图")
    print(f"  - full/marked/ 大图标注")
    print("=" * 60)
    try:
        from PyQt6.QtCore import QCoreApplication, QMetaObject, Qt
        app = QCoreApplication.instance()
        if app is not None:
            QMetaObject.invokeMethod(
                app, "quit", Qt.ConnectionType.QueuedConnection)
    except Exception as e:
        print(f"[OneLap] post quit 异常: {e}")


# Monkey-patch demo 模块
demo_ui_mock_camera.MockBVCamera.__init__ = _new_init
demo_ui_mock_camera.MockBVCamera.trigger_and_grab = _new_trigger_and_grab
demo_ui_mock_camera._on_lap_complete = _quit_after_one_lap

# 不清盘 — 保留已有训练数据
def _noop_cleanup(*args, **kwargs):
    return (0, 0)
demo_ui_mock_camera._cleanup_sirod_disk = _noop_cleanup


if __name__ == "__main__":
    print("=" * 60)
    print("test_image 全集 UI 演示 — 跑完所有 .bmp 自动退出")
    print(f"数据源: {_TEST_IMAGE_DIR}/*.bmp")
    print("存图保留在 D:\\SiRod\\ 给你训练用")
    print("=" * 60)
    sys.exit(demo_ui_mock_camera.main())
