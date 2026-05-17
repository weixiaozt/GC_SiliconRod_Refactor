"""
诊断 — 跑 12 张图但不装 lap_done 回调（不退出），看能否过第 10 张。

如果能过 → 卡死是 quit 路径触发的（callback / aboutToQuit / engine.stop 链）
如果不能过 → 卡死跟 callback 无关，另有真凶
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

import demo_ui_mock_camera


_TEST_IMAGE_DIR = _REPO_ROOT / "test_image"


def _new_init(self, uid: int = 0, **kwargs):
    self.model = "Mock-BV-C3110GE"
    self.serial = "MOCK001"
    self._streaming = False
    import os
    limit = int(os.environ.get("DEMO_IMAGE_LIMIT", "12"))
    all_paths = sorted(_TEST_IMAGE_DIR.glob("*.bmp"))
    self._image_paths = all_paths[:limit] if limit > 0 else all_paths
    self._idx = 0
    self._lap = 0
    self._lock = threading.Lock()
    print(f"[Diag] 数据源: {len(self._image_paths)} 张")


def _new_trigger_and_grab(self, timeout_ms: int = 5000):
    if not self._streaming:
        raise RuntimeError("not streaming")
    time.sleep(0.2)
    with self._lock:
        path = self._image_paths[self._idx]
        next_idx = self._idx + 1
        lap_done = next_idx >= len(self._image_paths)
        self._idx = 0 if lap_done else next_idx
        if lap_done:
            self._lap += 1
    img = imread_safe(path, cv2.IMREAD_UNCHANGED)
    print(f"[Diag] 触发 → {path.name}" + (f" [圈 {self._lap}]" if lap_done else ""))
    # 关键差异：不调 callback，继续跑下一圈
    return img


# 不清盘
def _noop_cleanup(*a, **kw):
    return (0, 0)

demo_ui_mock_camera.MockBVCamera.__init__ = _new_init
demo_ui_mock_camera.MockBVCamera.trigger_and_grab = _new_trigger_and_grab
demo_ui_mock_camera.MockBVCamera.on_lap_complete = None     # ★ 不装 callback
demo_ui_mock_camera._cleanup_sirod_disk = _noop_cleanup


if __name__ == "__main__":
    print("=" * 60)
    print("诊断模式 — 12 张 / 不退出 / 看能否过第 10 张")
    print("=" * 60)
    sys.exit(demo_ui_mock_camera.main())
