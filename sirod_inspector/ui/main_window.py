"""
主窗口 - TopBar + QStackedWidget + BottomBar
=============================================
匹配截图样式：
  TopBar: ◆ SiRod Inspector | 总览 历史记录 缺陷图库 统计报表 系统设置 | 状态徽章 时间 产线
  BottomBar: 设备状态指示灯 | 已接收计数
"""

import datetime
import logging
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QStackedWidget, QFrame,
)

logger = logging.getLogger("SiRod.MainWindow")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SiRod Inspector - 光伏方棒隐裂检测系统")
        self.setMinimumSize(1280, 720)
        self._pages = {}
        self._nav_buttons = []
        self._init_ui()
        self._start_clock()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ═══════ TopBar ═══════
        self._topbar = QFrame()
        self._topbar.setObjectName("TopBar")
        topbar_layout = QHBoxLayout(self._topbar)
        topbar_layout.setContentsMargins(16, 0, 16, 0)
        topbar_layout.setSpacing(0)

        # 菱形图标 + 标题
        icon_label = QLabel("◆")
        icon_label.setStyleSheet(
            "color: #00d4ff; font-size: 18px; padding-right: 6px;"
            " background: transparent; border: none;"
        )
        topbar_layout.addWidget(icon_label)

        title = QLabel("SiRod Inspector")
        title.setObjectName("AppTitle")
        topbar_layout.addWidget(title)
        topbar_layout.addSpacing(30)

        # 导航按钮：「参数」「日志」「相机」放最后，默认隐藏；main_camera 模式会启用
        tab_names = ["总览", "历史记录", "缺陷图库", "统计报表",
                     "系统设置", "参数", "日志", "相机"]
        for i, name in enumerate(tab_names):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, idx=i: self._switch_page(idx))
            topbar_layout.addWidget(btn)
            self._nav_buttons.append(btn)
            if name in ("参数", "日志", "相机"):
                btn.setVisible(False)

        topbar_layout.addStretch()

        # 状态徽章
        self._status_badge = QLabel("就绪")
        self._status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_badge.setFixedHeight(24)
        self._status_badge.setStyleSheet(
            "background-color: #27ae60; color: white; font-weight: bold;"
            " border-radius: 4px; padding: 2px 12px; font-size: 11px;"
        )
        topbar_layout.addWidget(self._status_badge)
        topbar_layout.addSpacing(16)

        # 时间
        self._clock_label = QLabel()
        self._clock_label.setStyleSheet(
            "color: #8888a0; font-size: 12px; background: transparent; border: none;"
        )
        topbar_layout.addWidget(self._clock_label)
        topbar_layout.addSpacing(16)

        # 产线标签
        self._line_label = QLabel("产线 PV-B02")
        self._line_label.setStyleSheet(
            "color: #8888a0; font-size: 12px; background: transparent; border: none;"
        )
        topbar_layout.addWidget(self._line_label)

        main_layout.addWidget(self._topbar)

        # ═══════ 内容区 ═══════
        self._stack = QStackedWidget()
        main_layout.addWidget(self._stack, 1)

        # ═══════ BottomBar ═══════
        self._bottombar = QFrame()
        self._bottombar.setObjectName("BottomBar")
        bottom_layout = QHBoxLayout(self._bottombar)
        bottom_layout.setContentsMargins(16, 0, 16, 0)
        bottom_layout.setSpacing(16)

        self._status_labels = {}
        # ★ Round 9 ★ 加"扫码枪"独立状态灯（之前误把扫码枪状态贴在"飞书"灯上）
        # 加"相机"灯：相机模式下反映 BVCamera 连接 / grab 状态（掉电掉线转红）
        for name in ["TCP", "相机", "数据库", "扫码枪", "飞书", "报警灯", "Run.bat"]:
            lbl = QLabel(f"● {name}")
            lbl.setStyleSheet(
                "color: #e74c3c; font-size: 11px;"
                " background: transparent; border: none;"
            )
            bottom_layout.addWidget(lbl)
            self._status_labels[name] = lbl

        bottom_layout.addStretch()

        self._recv_label = QLabel("已接收: 0")
        self._recv_label.setStyleSheet(
            "color: #8888a0; font-size: 11px;"
            " background: transparent; border: none;"
        )
        bottom_layout.addWidget(self._recv_label)

        main_layout.addWidget(self._bottombar)

        # 默认选中第一个导航
        if self._nav_buttons:
            self._nav_buttons[0].setChecked(True)

    # ─────────── 页面管理 ───────────
    def add_page(self, name, widget):
        idx = self._stack.addWidget(widget)
        self._pages[name] = idx

    def get_page(self, index):
        return self._stack.widget(index)

    def _switch_page(self, index):
        self._stack.setCurrentIndex(index)
        for i, btn in enumerate(self._nav_buttons):
            btn.setChecked(i == index)
        logger.debug(f"切换到页面: {index}")

    # ─────────── 状态更新 ───────────
    def set_device_status(self, device, connected):
        if device in self._status_labels:
            color = "#27ae60" if connected else "#e74c3c"
            self._status_labels[device].setStyleSheet(
                f"color: {color}; font-size: 11px;"
                f" background: transparent; border: none;"
            )

    def set_status_visible(self, device: str, visible: bool):
        """显示/隐藏底部某个设备状态灯（Halcon 模式没有相机/扫码枪灯）"""
        if device in self._status_labels:
            self._status_labels[device].setVisible(visible)

    def set_status_badge(self, text, color="#27ae60"):
        self._status_badge.setText(text)
        self._status_badge.setStyleSheet(
            f"background-color: {color}; color: white; font-weight: bold;"
            f" border-radius: 4px; padding: 2px 12px; font-size: 11px;"
        )

    def update_recv_count(self, count):
        self._recv_label.setText(f"已接收: {count}")

    def set_tab_visible(self, name: str, visible: bool):
        """显示/隐藏顶部某个导航按钮（按钮文字匹配 name）"""
        for btn in self._nav_buttons:
            if btn.text() == name:
                btn.setVisible(visible)
                break

    def set_line_id(self, line_id: str):
        self._line_label.setText(f"产线 {line_id}")

    # ─────────── 时钟 ───────────
    def _start_clock(self):
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

    def _update_clock(self):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._clock_label.setText(now)
