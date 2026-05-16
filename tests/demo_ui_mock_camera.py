"""
模拟相机驱动的 UI 启动器
===========================
不连真相机，把 source_image/*.tif 循环读出来当作"软触发抓到的帧"，
然后启 main_camera.py 的完整 UI + 流水线 + 存图。

效果：UI 里能看到每 2 秒一次的"抓图 → 检测 → 存图"，完全跟生产环境一样，
只是数据源是文件 instead of 相机。

用途：
  - 演示 UI / 给客户看效果
  - 没现场相机时调 UI / 调存图逻辑
  - 用一组已知图验证 pipeline 端到端
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

# ── 必须在 import sirod_inspector 之前 patch BVCamera ──

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imread_safe
setup_console_utf8()

import cv2
import numpy as np


# ============================================================
# Mock BV 相机：实现 BVCamera 同款接口，但从文件循环读图
# ============================================================

class MockBVCamera:
    """模拟 BVCamera：每次 ``trigger_and_grab`` 从 source_image 循环返回下一张图

    每完成一圈后会触发 ``_on_lap_complete`` 回调，可用于自动清理 D:/SiRod
    防止长跑硬盘爆满。
    """

    # 回调：每跑完一圈触发（类级属性，便于外部 patch）
    on_lap_complete = None      # callable(lap_index, total_images) | None

    def __init__(self, uid: int = 0, **kwargs):
        self.model = "Mock-BV-C3110GE"
        self.serial = "MOCK001"
        self._streaming = False
        # 加载所有 source_image .tif（uint16 大图）
        self._images: list[np.ndarray] = []
        self._image_names: list[str] = []
        src_dir = _REPO_ROOT / "source_image"
        for p in sorted(src_dir.glob("*.tif")):
            img = imread_safe(p, cv2.IMREAD_UNCHANGED)
            if img is None:
                continue
            self._images.append(img)
            self._image_names.append(p.name)
        if not self._images:
            raise RuntimeError(f"未找到 source_image/*.tif，路径: {src_dir}")
        self._idx = 0
        self._lap = 0
        self._lock = threading.Lock()
        print(f"[MockBVCamera] 加载 {len(self._images)} 张图作为模拟数据源:")
        for i, name in enumerate(self._image_names):
            print(f"  [{i}] {name}  shape={self._images[i].shape}  "
                  f"dtype={self._images[i].dtype}")

    def configure(self, **kwargs):
        # 模拟模式：参数全部忽略
        pass

    def start(self):
        self._streaming = True

    def stop(self):
        self._streaming = False

    def close(self):
        self.stop()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @property
    def is_streaming(self):
        return self._streaming

    def get_size(self):
        img = self._images[0]
        return img.shape[1], img.shape[0]

    def trigger_and_grab(self, timeout_ms: int = 5000) -> np.ndarray:
        """循环返回 source_image 里的下一张图，模拟 1s 抓图延迟"""
        if not self._streaming:
            raise RuntimeError("MockBVCamera 未启动")
        # 模拟相机抓图耗时
        time.sleep(0.8)
        with self._lock:
            img = self._images[self._idx]
            name = self._image_names[self._idx]
            next_idx = self._idx + 1
            lap_done = next_idx >= len(self._images)
            self._idx = 0 if lap_done else next_idx
            if lap_done:
                self._lap += 1
        # 返回 copy，避免下游误改
        print(f"[MockBVCamera] 触发 → 返回 {name}"
              + (f"  [圈 {self._lap} 已完成]" if lap_done else ""))
        # 一圈完成回调（在持锁外调用，避免回调里阻塞影响下次触发）
        if lap_done and MockBVCamera.on_lap_complete is not None:
            try:
                MockBVCamera.on_lap_complete(self._lap, len(self._images))
            except Exception as e:
                print(f"[MockBVCamera] on_lap_complete 回调异常: {e}")
        return img.copy()

    # 兼容 BVCamera 的 feature CRUD（设过就吞掉）
    def set_int(self, *a, **kw): pass
    def get_int(self, *a, **kw): return 0
    def set_float(self, *a, **kw): pass
    def get_float(self, *a, **kw): return 0.0
    def set_string(self, *a, **kw): pass
    def get_string(self, *a, **kw): return ""
    def set_enum(self, *a, **kw): pass
    def get_enum(self, *a, **kw): return ""
    def set_bool(self, *a, **kw): pass
    def execute(self, *a, **kw): pass


# ============================================================
# Monkey-patch + 启动 main_camera 主程序
# ============================================================

def _cleanup_sirod_disk(base_dir: Path = Path(r"D:\SiRod"),
                         keep_marked: bool = False) -> tuple:
    """自动清理 D:/SiRod 存图，防硬盘爆。

    Returns (deleted_bytes, deleted_count)
    """
    if not base_dir.is_dir():
        return 0, 0
    deleted_bytes = 0
    deleted_count = 0
    # 只清存图相关子目录，不动 base_dir 本身（防止误删用户其他东西）
    targets = ["ImageRaw", "WebImage", "images"]
    for sub in targets:
        d = base_dir / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    deleted_bytes += p.stat().st_size
                    p.unlink()
                    deleted_count += 1
                except Exception:
                    pass
        # 删空目录
        for d2 in sorted(d.rglob("*"), key=lambda x: -len(str(x))):
            try:
                if d2.is_dir():
                    d2.rmdir()
            except Exception:
                pass
    return deleted_bytes, deleted_count


def _on_lap_complete(lap: int, n_images: int):
    """每跑完一圈 source_image，清一次 D:/SiRod"""
    bytes_freed, n = _cleanup_sirod_disk()
    print(f"[AutoCleanup] 圈 {lap} 完成（{n_images} 张），"
          f"清理 {n} 个文件 ({bytes_freed/1024/1024:.0f} MB)")


def main() -> int:
    print("=" * 60)
    print("Mock 相机模式：用 source_image/*.tif 模拟相机喂 UI")
    print("=" * 60)

    # 启动前清一次（防上次残留)
    bytes_freed, n = _cleanup_sirod_disk()
    if n > 0:
        print(f"[AutoCleanup] 启动前清理 {n} 个旧文件 "
              f"({bytes_freed/1024/1024:.0f} MB)")

    # 注册回调：每跑完一圈清一次
    MockBVCamera.on_lap_complete = _on_lap_complete

    # 关键：main_camera.py 内部用 `from core.inspect_engine import ...`，
    # 所以要 patch 的是 `core.inspect_engine`（不是 sirod_inspector.core.inspect_engine）
    # — Python 把同一个文件用两种 import 路径当两份独立 module
    import importlib
    engine_mod = importlib.import_module("core.inspect_engine")
    engine_mod.BVCamera = MockBVCamera

    # 同样要 patch `camera` 模块（main_camera 走的 import 路径）
    from sirod_inspector.camera.bv_camera import BVCameraDevice
    def fake_enumerate():
        return [BVCameraDevice(
            uid=0x1234567890ABCDEF, bus_number=0,
            device_type=1,
            vendor="Mock", model="Mock-BV-C3110GE",
            serial="MOCK001", ip_addr="127.0.0.1",
        )]
    # 也 patch sirod_inspector.* 那份（保险）
    for name in ("core.inspect_engine", "sirod_inspector.core.inspect_engine",
                 "camera", "sirod_inspector.camera"):
        try:
            m = importlib.import_module(name)
            m.BVCamera = MockBVCamera
            m.enumerate_devices = fake_enumerate
            print(f"  patched {name}: BVCamera = MockBVCamera")
        except Exception as e:
            print(f"  patch {name} 失败: {e}")

    # 进入主程序
    from main_camera import main as main_camera_main
    return main_camera_main()


if __name__ == "__main__":
    sys.exit(main())
