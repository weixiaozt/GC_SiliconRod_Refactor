"""
总览页面
========
匹配设计截图布局：
  Row 1: 4 个统计卡片（检测数量 / 合格数量 / NG 数量 / 平均检测时长）
  Row 2: 晶棒编号栏（编号 + 状态徽章 + 更新扫码按钮）
  Row 3: NIR 实时预览区（相机参数标签 + 无缺陷/LIVE 状态 + 大面积图像）
"""

import logging
import datetime
import time
import numpy as np

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot, QSize, QPointF, QRectF
from PyQt6.QtGui import (
    QImage, QPixmap, QFont, QPainter, QWheelEvent,
    QMouseEvent, QTransform,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QPushButton, QSizePolicy, QSpacerItem,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QCheckBox,
)

logger = logging.getLogger("SiRod.Overview")


# ─────────────────────────────────────────────
#  可缩放拖拽的图像查看器
# ─────────────────────────────────────────────
class ZoomableImageViewer(QGraphicsView):
    """
    支持鼠标滚轮缩放和拖拽平移的图像查看器。

    功能：
      - 鼠标滚轮：以光标位置为中心缩放图像
      - 鼠标左键拖拽：平移图像
      - 双击左键：重置为适应窗口大小（Fit）
      - 右键双击：重置为 1:1 原始大小
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # 场景
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        # 图像项
        self._pixmap_item = QGraphicsPixmapItem()
        self._scene.addItem(self._pixmap_item)

        # 缩放参数
        self._zoom_factor = 1.15       # 每次滚轮缩放倍率
        self._min_zoom = 0.05          # 最小缩放比
        self._max_zoom = 50.0          # 最大缩放比
        self._current_zoom = 1.0       # 当前缩放级别

        # 拖拽状态
        self._dragging = False
        self._drag_start = QPointF()

        # 外观设置
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter
        )
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # 暗色背景
        self.setStyleSheet(
            "QGraphicsView {"
            "  background-color: #0d1b2a;"
            "  border: none;"
            "}"
        )

        # 占位文字
        self._placeholder_text = "等待相机连接..."
        self._has_image = False

    # ─── 公开接口 ───

    def set_image(self, pixmap: QPixmap):
        """设置新图像并自适应显示"""
        self._pixmap_item.setPixmap(pixmap)
        self._has_image = True
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.fit_in_view()

    def clear_image(self):
        """清除图像"""
        self._pixmap_item.setPixmap(QPixmap())
        self._has_image = False
        self._current_zoom = 1.0
        self.resetTransform()
        self.viewport().update()

    def set_placeholder_text(self, text: str):
        """设置无图像时的占位文字"""
        self._placeholder_text = text
        if not self._has_image:
            self.viewport().update()

    def fit_in_view(self):
        """将图像缩放到适应视图大小"""
        if not self._has_image:
            return
        self.resetTransform()
        self._current_zoom = 1.0
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        # 记录实际缩放级别
        self._current_zoom = self.transform().m11()

    def reset_to_original(self):
        """重置为 1:1 原始大小"""
        if not self._has_image:
            return
        self.resetTransform()
        self._current_zoom = 1.0
        # 居中显示
        self.centerOn(self._pixmap_item)

    # ─── 事件处理 ───

    def wheelEvent(self, event: QWheelEvent):
        """鼠标滚轮缩放（以光标位置为中心）"""
        if not self._has_image:
            return

        # 计算缩放方向
        angle = event.angleDelta().y()
        if angle > 0:
            factor = self._zoom_factor
        elif angle < 0:
            factor = 1.0 / self._zoom_factor
        else:
            return

        # 检查缩放范围
        new_zoom = self._current_zoom * factor
        if new_zoom < self._min_zoom or new_zoom > self._max_zoom:
            return

        self._current_zoom = new_zoom
        self.scale(factor, factor)

    def mousePressEvent(self, event: QMouseEvent):
        """鼠标按下 — 开始拖拽"""
        if event.button() == Qt.MouseButton.LeftButton and self._has_image:
            self._dragging = True
            self._drag_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        """鼠标移动 — 拖拽平移"""
        if self._dragging:
            delta = event.position() - self._drag_start
            self._drag_start = event.position()
            # 平移视图
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        """鼠标释放 — 结束拖拽"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """双击重置视图"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.fit_in_view()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self.reset_to_original()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):
        """窗口大小变化时自适应"""
        super().resizeEvent(event)
        if self._has_image:
            # 仅在未手动缩放时自适应
            pass

    def paintEvent(self, event):
        """绘制占位文字"""
        super().paintEvent(event)
        if not self._has_image:
            painter = QPainter(self.viewport())
            painter.setPen(Qt.GlobalColor.darkGray)
            font = QFont()
            font.setPointSize(14)
            painter.setFont(font)
            painter.drawText(
                self.viewport().rect(),
                Qt.AlignmentFlag.AlignCenter,
                self._placeholder_text,
            )
            painter.end()


# ─────────────────────────────────────────────
#  统计卡片组件
# ─────────────────────────────────────────────
class StatCard(QFrame):
    """单个统计卡片 — 上方标题，下方大号数值"""

    def __init__(self, title: str, value: str = "0", value_color: str = "#e0e0e0"):
        super().__init__()
        self.setObjectName("StatCard")
        self.setStyleSheet(
            "QFrame#StatCard {"
            "  background-color: #16213e;"
            "  border: 1px solid #0f3460;"
            "  border-radius: 6px;"
            "}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(4)

        # 标题
        self._title_label = QLabel(title)
        self._title_label.setStyleSheet(
            "color: #8888a0; font-size: 12px; background: transparent; border: none;"
        )
        layout.addWidget(self._title_label)

        # 数值
        self._value_label = QLabel(value)
        self._value_label.setStyleSheet(
            f"color: {value_color}; font-size: 36px; font-weight: bold;"
            f" background: transparent; border: none;"
        )
        layout.addWidget(self._value_label)

    def set_value(self, val: str):
        self._value_label.setText(val)

    def set_color(self, color: str):
        self._value_label.setStyleSheet(
            f"color: {color}; font-size: 36px; font-weight: bold;"
            f" background: transparent; border: none;"
        )


# ─────────────────────────────────────────────
#  总览页面
# ─────────────────────────────────────────────
class OverviewPage(QWidget):
    """总览页面 — 匹配设计截图"""

    # 跨线程安全信号
    new_data_signal = pyqtSignal(object)
    shift_reset_signal = pyqtSignal()   # 班次清零信号（从定时器线程安全传递到 UI）
    reset_requested = pyqtSignal()      # 用户点击"复位"按钮，由 main.py 连接并发送串口复位信号
    alarm_enabled_changed = pyqtSignal(bool)  # "启用报警"勾选变更，由 main.py 连接并持久化到 config

    def __init__(self):
        super().__init__()

        # 统计数据
        self._total = 0
        self._ok_count = 0
        self._ng_count = 0
        self._avg_ms = 0.0
        self._last_recv_time = None

        # 班次统计管理器（由 main.py 注入）
        self._shift_stats = None

        self._init_ui()
        self.new_data_signal.connect(self._on_new_data)
        self.shift_reset_signal.connect(self._on_shift_reset)

    @pyqtSlot()
    def _on_shift_reset(self):
        """班次清零信号槽函数（在 UI 线程中执行）"""
        logger.info("班次清零触发，重置统计数据")
        self.reset_stats()

    # ─────────── 报警开关 ───────────
    def _on_alarm_toggled(self, checked: bool):
        """勾选框状态变化，转发到 main.py 持久化"""
        logger.info(f"报警开关状态变更: {'启用' if checked else '禁用'}")
        self.alarm_enabled_changed.emit(checked)

    def is_alarm_enabled(self) -> bool:
        """供 main.py 查询当前报警开关状态"""
        return self._alarm_checkbox.isChecked()

    def set_alarm_enabled(self, enabled: bool):
        """由 main.py 在启动时根据 config 恢复勾选状态（不会触发信号的循环保存）"""
        # 用 blockSignals 避免恢复初始值时触发 toggled 信号再写回 config
        self._alarm_checkbox.blockSignals(True)
        self._alarm_checkbox.setChecked(bool(enabled))
        self._alarm_checkbox.blockSignals(False)

    # ─────────── MES 上传状态 ───────────
    def set_mes_status(self, success: bool, rod_id: str = "", message: str = ""):
        """更新 MES 上传状态标签。

        由 main.py 在 HTTP 请求完成后（通过 UI 线程信号）调用。
        success=True: 绿色；success=False: 红色；传 None 可表示"上传中"（橙色）。
        """
        rod_short = rod_id[:10] + "…" if rod_id and len(rod_id) > 10 else (rod_id or "")
        time_str = datetime.datetime.now().strftime("%H:%M:%S")

        if success is None:
            bg, fg = "#f39c12", "white"
            text = f"MES: 上传中 {rod_short}"
        elif success:
            bg, fg = "#27ae60", "white"
            text = f"MES: ✓ {rod_short} {time_str}"
        else:
            bg, fg = "#e74c3c", "white"
            short_msg = (message[:20] + "…") if message and len(message) > 20 else message
            text = f"MES: ✗ {rod_short} {short_msg}"

        self._mes_status_label.setText(text)
        self._mes_status_label.setToolTip(
            f"最近一次 MES 上传\n棒号: {rod_id or '-'}\n"
            f"时间: {time_str}\n结果: {message or ('成功' if success else '未知')}"
        )
        self._mes_status_label.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-size: 11px;"
            f" font-weight: bold; border-radius: 4px; padding: 2px 8px;"
        )

    # ─────────── UI 构建 ───────────
    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(8)

        # ── Row 1: 统计卡片 ──
        cards_row = QHBoxLayout()
        cards_row.setSpacing(10)

        self._card_total = StatCard("检测数量", "0", "#00d4ff")
        self._card_ok    = StatCard("合格数量", "0", "#27ae60")
        self._card_ng    = StatCard("NG 数量",  "0", "#e74c3c")
        self._card_time  = StatCard("平均检测时长", "-- ms", "#e0e0e0")

        for card in [self._card_total, self._card_ok, self._card_ng, self._card_time]:
            cards_row.addWidget(card, 1)
        root.addLayout(cards_row)

        # ── Row 2: 晶棒编号栏 ──
        rod_bar = QFrame()
        rod_bar.setObjectName("RodBar")
        rod_bar.setStyleSheet(
            "QFrame#RodBar {"
            "  background-color: #16213e;"
            "  border: 1px solid #0f3460;"
            "  border-radius: 6px;"
            "}"
        )
        rod_bar.setFixedHeight(48)

        rod_layout = QHBoxLayout(rod_bar)
        rod_layout.setContentsMargins(16, 0, 16, 0)
        rod_layout.setSpacing(12)

        rod_title = QLabel("晶棒编号")
        rod_title.setStyleSheet(
            "color: #8888a0; font-size: 13px; background: transparent; border: none;"
        )
        rod_layout.addWidget(rod_title)

        self._rod_id_label = QLabel("等待扫码 . . .")
        self._rod_id_label.setStyleSheet(
            "color: #f39c12; font-size: 18px; font-weight: bold;"
            " background: transparent; border: none;"
        )
        rod_layout.addWidget(self._rod_id_label)

        # 扫码状态徽章
        self._scan_badge = QLabel("未扫码")
        self._scan_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scan_badge.setFixedSize(64, 28)
        self._scan_badge.setStyleSheet(
            "background-color: #e74c3c; color: white; font-size: 12px;"
            " font-weight: bold; border-radius: 4px;"
        )
        rod_layout.addWidget(self._scan_badge)

        rod_layout.addStretch()

        # MES 上传状态标签（显示最近一次 NG 上传的结果）
        self._mes_status_label = QLabel("MES: 等待")
        self._mes_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mes_status_label.setMinimumSize(140, 24)
        self._mes_status_label.setToolTip("MES 接口最近一次 NG 上传结果")
        self._mes_status_label.setStyleSheet(
            "background-color: #2c3e50; color: #bbbbcc; font-size: 11px;"
            " font-weight: bold; border-radius: 4px; padding: 2px 8px;"
        )
        rod_layout.addWidget(self._mes_status_label)

        # 启用报警勾选框（勾选时 NG 会发串口信号+弹窗；取消勾选则静默处理）
        self._alarm_checkbox = QCheckBox("启用报警")
        self._alarm_checkbox.setChecked(True)  # 默认启用
        self._alarm_checkbox.setToolTip(
            "勾选：检测到 NG 时发送串口报警信号并弹窗\n"
            "不勾选：NG 数据照常记录到数据库/统计/图库，但不触发报警"
        )
        self._alarm_checkbox.setStyleSheet(
            "QCheckBox { color: #e0e0e0; font-size: 13px;"
            " background: transparent; border: none; padding: 0 6px; }"
            "QCheckBox::indicator { width: 16px; height: 16px; }"
            "QCheckBox::indicator:unchecked {"
            " background-color: #1a1a2e; border: 1px solid #555566; border-radius: 3px; }"
            "QCheckBox::indicator:checked {"
            " background-color: #27ae60; border: 1px solid #2ecc71; border-radius: 3px;"
            " image: url(none); }"
            "QCheckBox:hover { color: #00d4ff; }"
        )
        self._alarm_checkbox.toggled.connect(self._on_alarm_toggled)
        rod_layout.addWidget(self._alarm_checkbox)

        # 更新扫码按钮
        self._rescan_btn = QPushButton("更新扫码")
        self._rescan_btn.setFixedSize(90, 30)
        self._rescan_btn.setStyleSheet(
            "QPushButton { background-color: #0f3460; color: #e0e0e0;"
            " border: 1px solid #1a5276; border-radius: 4px; font-size: 12px; }"
            "QPushButton:hover { background-color: #1a5276; }"
        )
        rod_layout.addWidget(self._rescan_btn)

        # 复位按钮（发送串口复位信号）
        self._reset_btn = QPushButton("复  位")
        self._reset_btn.setObjectName("ResetBtn")
        self._reset_btn.setFixedSize(90, 30)
        self._reset_btn.setToolTip("向报警灯/PLC 发送复位信号（内容在设置页配置）")
        self._reset_btn.setStyleSheet(
            "QPushButton#ResetBtn { background-color: #e67e22; color: white;"
            " border: 1px solid #d35400; border-radius: 4px;"
            " font-size: 12px; font-weight: bold; }"
            "QPushButton#ResetBtn:hover  { background-color: #d35400; }"
            "QPushButton#ResetBtn:pressed{ background-color: #a04000; }"
        )
        self._reset_btn.clicked.connect(self.reset_requested.emit)
        rod_layout.addWidget(self._reset_btn)

        root.addWidget(rod_bar)

        # ── Row 3: NIR 实时预览区 ──
        # 预览区标题行
        preview_header = QHBoxLayout()
        preview_header.setSpacing(10)

        nir_title = QLabel("NIR 实时预览")
        nir_title.setStyleSheet(
            "color: #e0e0e0; font-size: 14px; font-weight: bold;"
        )
        preview_header.addWidget(nir_title)

        # 当前检测信息标签（动态更新，替换原来的固定相机参数标签）
        self._info_rod_label = self._make_tag("晶棒编号: --", "#8888a0")
        self._info_result_label = self._make_tag("检测结果: --", "#8888a0")
        self._info_defect_label = self._make_tag("缺陷类型: --", "#8888a0")
        self._info_time_label   = self._make_tag("耸时: -- ms", "#8888a0")

        for lbl in [self._info_rod_label, self._info_result_label,
                    self._info_defect_label, self._info_time_label]:
            preview_header.addWidget(lbl)

        preview_header.addStretch()

        # 缺陷状态标签
        self._defect_status_label = QLabel("无缺陷")
        self._defect_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._defect_status_label.setFixedSize(64, 24)
        self._defect_status_label.setStyleSheet(
            "background-color: #27ae60; color: white; font-size: 11px;"
            " font-weight: bold; border-radius: 3px;"
        )
        preview_header.addWidget(self._defect_status_label)

        # LIVE 标签
        self._live_label = QLabel("LIVE")
        self._live_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._live_label.setFixedSize(50, 24)
        self._live_label.setStyleSheet(
            "background-color: #e74c3c; color: white; font-size: 11px;"
            " font-weight: bold; border-radius: 3px;"
        )
        preview_header.addWidget(self._live_label)

        root.addLayout(preview_header)

        # 图像预览区 — 使用可缩放的图像查看器
        img_frame = QFrame()
        img_frame.setObjectName("ImageFrame")
        img_frame.setStyleSheet(
            "QFrame#ImageFrame {"
            "  background-color: #0d1b2a;"
            "  border: 1px solid #0f3460;"
            "  border-radius: 6px;"
            "}"
        )
        img_layout = QVBoxLayout(img_frame)
        img_layout.setContentsMargins(2, 2, 2, 2)

        self._image_viewer = ZoomableImageViewer()
        self._image_viewer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        img_layout.addWidget(self._image_viewer)

        # 保留 _image_label 引用以兼容 reset_stats 等方法
        self._image_label = self._image_viewer

        root.addWidget(img_frame, 1)   # stretch=1 让图像区占满剩余空间

    # ─────────── 辅助方法 ───────────
    @staticmethod
    def _make_tag(text: str, color: str) -> QLabel:
        """创建一个小标签（相机参数 tag）"""
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {color}; font-size: 11px; background-color: #1e2d45;"
            f" border: 1px solid #0f3460; border-radius: 3px;"
            f" padding: 2px 8px;"
        )
        return lbl

    # ─────────── 公开接口 ───────────
    def on_inspect_data(self, data):
        """TCP 线程调用 → 通过信号安全传递到 UI 线程"""
        self.new_data_signal.emit(data)

    def set_camera_info(self, model: str = "", resolution: str = "",
                        bit_depth: str = "", wavelength: str = ""):
        """兼容接口（原相机参数标签已替换为检测信息标签，此方法保留避免报错）"""
        pass

    def set_live(self, is_live: bool):
        """设置 LIVE 标签状态"""
        if is_live:
            self._live_label.setStyleSheet(
                "background-color: #e74c3c; color: white; font-size: 11px;"
                " font-weight: bold; border-radius: 3px;"
            )
            self._live_label.setText("LIVE")
        else:
            self._live_label.setStyleSheet(
                "background-color: #555566; color: #aaaaaa; font-size: 11px;"
                " font-weight: bold; border-radius: 3px;"
            )
            self._live_label.setText("OFF")

    def set_shift_stats(self, shift_stats):
        """
        注入 ShiftStats 管理器。

        由 main.py 在初始化时调用，用于：
          - 启动时恢复上次统计数据
          - 每次数据更新后持久化
          - 班次清零时同步
        """
        self._shift_stats = shift_stats
        # 恢复上次统计数据
        self._total = shift_stats.total
        self._ok_count = shift_stats.ok_count
        self._ng_count = shift_stats.ng_count
        self._avg_ms = shift_stats.avg_ms
        # 刷新 UI 显示
        self._card_total.set_value(str(self._total))
        self._card_ok.set_value(str(self._ok_count))
        self._card_ng.set_value(str(self._ng_count))
        if self._avg_ms > 0:
            self._card_time.set_value(f"{self._avg_ms:.0f} ms")
        logger.info(
            f"统计数据已恢复: 检测={self._total}, "
            f"合格={self._ok_count}, NG={self._ng_count}"
        )

    def reset_stats(self):
        """重置统计数据（班次清零或手动重置）"""
        self._total = 0
        self._ok_count = 0
        self._ng_count = 0
        self._avg_ms = 0.0
        self._last_recv_time = None
        self._card_total.set_value("0")
        self._card_ok.set_value("0")
        self._card_ng.set_value("0")
        self._card_time.set_value("-- ms")
        self._rod_id_label.setText("等待扫码 . . .")
        self._scan_badge.setText("未扫码")
        self._scan_badge.setStyleSheet(
            "background-color: #e74c3c; color: white; font-size: 12px;"
            " font-weight: bold; border-radius: 4px;"
        )
        self._defect_status_label.setText("无缺陷")
        self._defect_status_label.setStyleSheet(
            "background-color: #27ae60; color: white; font-size: 11px;"
            " font-weight: bold; border-radius: 3px;"
        )
        self._image_viewer.clear_image()
        self._image_viewer.set_placeholder_text("等待相机连接...")
        # 同步持久化
        if self._shift_stats:
            self._shift_stats.reset()

    # ─────────── 内部槽函数 ───────────
    @pyqtSlot(object)
    def _on_new_data(self, data):
        """在 UI 线程中处理新到达的检测数据"""
        recv_time = time.time()

        # 统计计数
        self._total += 1
        if data.result == "OK":
            self._ok_count += 1
        else:
            self._ng_count += 1

        # 计算平均检测时长（两次数据间隔近似）
        if self._last_recv_time is not None:
            delta_ms = (recv_time - self._last_recv_time) * 1000
            # 滑动平均
            self._avg_ms = (self._avg_ms * (self._total - 1) + delta_ms) / self._total
        self._last_recv_time = recv_time

        # 更新统计卡片
        self._card_total.set_value(str(self._total))
        self._card_ok.set_value(str(self._ok_count))
        self._card_ng.set_value(str(self._ng_count))
        if self._avg_ms > 0:
            self._card_time.set_value(f"{self._avg_ms:.0f} ms")

        # 持久化统计数据
        if self._shift_stats:
            self._shift_stats.update(
                self._total, self._ok_count, self._ng_count, self._avg_ms
            )

        # 更新预览区顶部检测信息标签
        rod_id = data.rod_id or "UNKNOWN"
        rod_short = (rod_id[:12] + "…") if len(rod_id) > 12 else rod_id
        self._info_rod_label.setText(f"晶棒: {rod_short}")
        result_color = "#e74c3c" if data.result == "NG" else "#27ae60"
        self._info_result_label.setText(f"结果: {data.result}")
        self._info_result_label.setStyleSheet(
            f"color: {result_color}; font-size: 11px; background-color: #1e2d45;"
            " border: 1px solid #0f3460; border-radius: 3px; padding: 2px 8px;"
            " font-weight: bold;"
        )
        defect_str = data.defect_type or "-"
        self._info_defect_label.setText(f"缺陷: {defect_str}")
        ct_ms = int(getattr(data, 'ct', 0) * 1000) if getattr(data, 'ct', 0) else (
            int(self._avg_ms) if self._avg_ms > 0 else 0
        )
        self._info_time_label.setText(f"耗时: {ct_ms} ms")
        self._rod_id_label.setText(rod_id)
        self._rod_id_label.setStyleSheet(
            "color: #00d4ff; font-size: 18px; font-weight: bold;"
            " background: transparent; border: none;"
        )
        self._scan_badge.setText("已扫码")
        self._scan_badge.setStyleSheet(
            "background-color: #27ae60; color: white; font-size: 12px;"
            " font-weight: bold; border-radius: 4px;"
        )

        # 更新缺陷状态
        if data.result == "NG":
            defect_text = data.defect_type or "NG"
            self._defect_status_label.setText(defect_text)
            self._defect_status_label.setFixedSize(
                max(64, len(defect_text) * 10 + 20), 24
            )
            self._defect_status_label.setStyleSheet(
                "background-color: #e74c3c; color: white; font-size: 11px;"
                " font-weight: bold; border-radius: 3px;"
            )
        else:
            self._defect_status_label.setText("无缺陷")
            self._defect_status_label.setFixedSize(64, 24)
            self._defect_status_label.setStyleSheet(
                "background-color: #27ae60; color: white; font-size: 11px;"
                " font-weight: bold; border-radius: 3px;"
            )

        # 显示图像 — main_camera 已经在工作线程预渲染了 marked 挂到
        # data._marked_image（避免 UI 线程扛 draw_marked_full 的 100ms 卡顿）。
        # 没 marked 时退回 raw（兼容手动塞 InspectData 场景）。
        # 显式 is None 检查 — `arr or fallback` 在 ndarray 多元素时 raise
        marked = getattr(data, "_marked_image", None)
        display_img = marked if marked is not None else data.image
        if display_img is not None:
            self._display_image(display_img)

        logger.info(
            f"总览更新: total={self._total}, ok={self._ok_count}, "
            f"ng={self._ng_count}, rod={rod_id}, result={data.result}"
        )

    def _display_image(self, img_array: np.ndarray):
        """将 numpy 数组显示到可缩放图像查看器。

        ⚠ PyQt6 的 QImage(buffer, ...) 不持 numpy 数组引用。
        函数返回后 img_array 被 GC → QImage 内 ptr 悬空 → 后续 paint
        随机 segfault（进程崩，bash 显示 exit 127）。

        修复：QImage 创建后立即 .copy() — Qt 内部拷一份独立内存，
        和 numpy buffer 解耦。开销 ~3MB 拷贝（1024×3072 BGR），<5ms，
        在 UI 显示路径上不致命。
        """
        try:
            # 确保数组是连续内存布局
            img_array = np.ascontiguousarray(img_array)

            if img_array.ndim == 2:
                h, w = img_array.shape
                bytes_per_line = w
                qimg = QImage(
                    img_array.data, w, h, bytes_per_line,
                    QImage.Format.Format_Grayscale8,
                )
            elif img_array.ndim == 3:
                h, w, ch = img_array.shape
                if ch == 3:
                    bytes_per_line = 3 * w
                    # BGR (OpenCV 默认) → 用 Format_BGR888 直接显示，
                    # 不需要再做 cvtColor。之前用 Format_RGB888 → R/B 颠倒。
                    qimg = QImage(
                        img_array.data, w, h, bytes_per_line,
                        QImage.Format.Format_BGR888,
                    )
                elif ch == 4:
                    bytes_per_line = 4 * w
                    qimg = QImage(
                        img_array.data, w, h, bytes_per_line,
                        QImage.Format.Format_RGBA8888,
                    )
                else:
                    logger.warning(f"不支持的图像通道数: {ch}")
                    return
            else:
                logger.warning(f"不支持的图像维度: {img_array.ndim}")
                return

            # 关键：copy() — 让 Qt 独立持有像素数据，不依赖 numpy 生命周期
            qimg = qimg.copy()
            pixmap = QPixmap.fromImage(qimg)
            self._image_viewer.set_image(pixmap)

        except Exception as e:
            logger.error(f"显示图像失败: {e}", exc_info=True)