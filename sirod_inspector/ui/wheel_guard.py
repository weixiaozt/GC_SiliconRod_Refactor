"""
全局滚轮防误触
==============
PyQt 的 ``QSpinBox`` / ``QDoubleSpinBox`` / ``QDateEdit``（均继承 ``QAbstractSpinBox``）
和 ``QComboBox`` 默认吃鼠标滚轮 —— operator 想滚动页面查看参数时，鼠标只要悬停在
某个控件上，滚轮就会把它的值滚改了（判定阈值、相机参数这种危险参数尤其要命）。

本模块装一个 application 级事件过滤器，专门拦这两类控件的 ``Wheel`` 事件。

用法（在 QApplication 创建后）::

    app = QApplication(sys.argv)
    from ui.wheel_guard import install_wheel_guard
    install_wheel_guard(app)          # 一行搞定，内部已处理防 GC

设计说明
--------
- **拦截目标**：``QAbstractSpinBox``（含 SpinBox/DoubleSpinBox/DateEdit/TimeEdit）+ ``QComboBox``
- **不影响**：图像查看器的滚轮缩放（自定义 ``wheelEvent`` 的 widget，不属这两类）、
  滚动条、列表/表格/滚动区的滚动 —— 这些照常工作。
- **值仍可改**：点击控件后用 键盘上下键 / 控件右侧上下箭头 / 直接键入。
- 取舍：鼠标正悬停在受拦控件上时，滚轮被吞掉、不会滚动页面（事件被消费）。
  这是工业 HMI 最常用的做法 —— 想滚页面把鼠标移到空白处或用滚动条即可。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QEvent
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox


class WheelGuard(QObject):
    """事件过滤器：吞掉数字框/下拉框的滚轮事件，防误改参数。"""

    _TARGETS = (QAbstractSpinBox, QComboBox)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, self._TARGETS):
            return True          # 消费掉滚轮事件，控件不改值
        return super().eventFilter(obj, event)


def install_wheel_guard(app) -> WheelGuard:
    """给 QApplication 装上全局滚轮防误触过滤器。

    返回 WheelGuard 实例（已挂到 ``app._wheel_guard`` 防 GC —— installEventFilter
    不持强引用，不保留会被回收导致过滤器失效）。幂等：重复调用只装一次。
    """
    existing = getattr(app, "_wheel_guard", None)
    if isinstance(existing, WheelGuard):
        return existing
    guard = WheelGuard()
    app._wheel_guard = guard          # ★ 防 GC
    app.installEventFilter(guard)
    return guard
