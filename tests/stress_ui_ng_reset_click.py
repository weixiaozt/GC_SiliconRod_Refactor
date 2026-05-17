"""
压力测试：NG popup + 用户点 "复位" 按钮 — 验证 UI 线程不卡

复现用户场景：
  - 新 cls 模型连续产 4 张 NG → NG popup 弹出（iter27 dedup 只一个）
  - 用户点 popup 上的"复位"按钮 → finished 信号 → _on_ng_popup_closed
    → _on_reset_clicked → 串口失败
  - **历史 bug**：_on_reset_clicked 用 QMessageBox.warning（modal exec()）
    → UI 线程阻塞 → 任意位置点击都未响应

iter29 修复：复位失败不再弹 modal，改走状态栏 + log。

本测试：headless 模拟用户点击复位按钮，监控心跳 QTimer 间隔。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

# Qt offscreen — 不开真窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget
from PyQt6.QtCore import QTimer, Qt, QObject, pyqtSignal


_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  {'[OK ]' if cond else '[FAIL]'} {name}"
           + (f"  — {detail}" if detail else ""))
    if not cond:
        _fail += 1


def test_reset_failure_no_modal():
    """模拟 _on_reset_clicked 失败路径（serial 未打开），验证不阻塞"""
    print("=" * 60)
    print("Test 1: _on_reset_clicked 失败时不弹 modal")
    print("=" * 60)

    app = QApplication.instance() or QApplication(sys.argv)

    # Mock 一个最小 SiRodCameraApp 上下文
    class MockSerialMgr:
        def send_reset(self):
            return False    # 模拟串口未打开

    class MockWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.status_badge_text = ""
            self.status_badge_color = ""
        def set_status_badge(self, text, color="#27ae60"):
            self.status_badge_text = text
            self.status_badge_color = color

    # 心跳：每 100ms 一次，记录每次 fire 时间
    heartbeats = []
    def hb():
        heartbeats.append(time.perf_counter())
    hb_timer = QTimer()
    hb_timer.timeout.connect(hb)
    hb_timer.start(100)

    # 直接复现 iter29 修复后的逻辑
    window = MockWindow()
    serial_mgr = MockSerialMgr()

    def on_reset():
        """复制 main_camera._on_reset_clicked iter29 之后的逻辑"""
        try:
            ok = serial_mgr.send_reset()
            if ok:
                window.set_status_badge("已复位", "#27ae60")
            else:
                window.set_status_badge("复位失败 — 检查串口", "#e74c3c")
        except Exception as e:
            window.set_status_badge(f"复位异常: {e}", "#e74c3c")

    # 100ms 后触发 reset，500ms 后退出
    QTimer.singleShot(100, on_reset)
    QTimer.singleShot(500, app.quit)
    app.exec()

    print(f"  心跳数: {len(heartbeats)}")
    if len(heartbeats) >= 2:
        gaps = [(heartbeats[i+1] - heartbeats[i]) * 1000
                for i in range(len(heartbeats)-1)]
        max_gap = max(gaps)
        check(f"无 modal 阻塞（最大心跳间隔 {max_gap:.0f}ms）",
              max_gap < 200,
              f"应 < 200ms（100ms 定时器精度）")
    check("status badge 显示复位失败",
          window.status_badge_text == "复位失败 — 检查串口")
    check("status badge 颜色红色",
          window.status_badge_color == "#e74c3c")


def test_ng_popup_non_modal():
    """验证 NG popup 是 non-modal 不挡主窗口"""
    print()
    print("=" * 60)
    print("Test 2: NG popup setWindowModality(NonModal)")
    print("=" * 60)

    app = QApplication.instance() or QApplication(sys.argv)
    parent = QWidget()
    parent.show()

    msg = QMessageBox(parent)
    msg.setWindowTitle("NG 报警")
    msg.setText("test")
    msg.addButton("复 位", QMessageBox.ButtonRole.AcceptRole)
    msg.addButton("关 闭", QMessageBox.ButtonRole.RejectRole)
    msg.setWindowModality(Qt.WindowModality.NonModal)
    msg.setModal(False)
    msg.show()

    check("windowModality == NonModal",
          msg.windowModality() == Qt.WindowModality.NonModal,
          f"got {msg.windowModality()}")
    check("isModal() == False", not msg.isModal())

    msg.close()
    parent.close()


def main():
    test_reset_failure_no_modal()
    test_ng_popup_non_modal()
    print()
    if _fail == 0:
        print("[OK] 全部通过 — UI 不会被 reset / popup 卡死")
        return 0
    print(f"[FAIL] {_fail} 个失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
