"""
日志查看页（实时滚动 + 等级过滤）
================================
作为一个 logging.Handler 接到 root logger，所有 ``logger.info/warning/error``
都会实时出现在这个 tab 里。线程安全（用 pyqtSignal 跨线程）。

UI::

    [✓ INFO]  [✓ WARN]  [✓ ERROR]              [✓ 自动滚动]  [清屏]
    ┌───────────────────────────────────────────────────────────┐
    │ 12:00:01 [INFO    ] SiRod.MainCamera: 应用启动            │
    │ 12:00:03 [WARNING ] SiRod.Scanner: 扫码枪连接失败         │
    │ ...                                                       │
    └───────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
from collections import deque

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPlainTextEdit, QCheckBox, QPushButton,
)


# 不同等级配色
LEVEL_COLOR = {
    "DEBUG":    "#7f8c8d",
    "INFO":     "#bbbbcc",
    "WARNING":  "#f39c12",
    "ERROR":    "#e74c3c",
    "CRITICAL": "#c0392b",
}


# ============================================================
# logging.Handler — 把 log 转发到 LogPage（跨线程安全）
# ============================================================

class _UiLogHandler(logging.Handler):
    def __init__(self, sink_fn):
        super().__init__()
        self._sink = sink_fn

    def emit(self, record):
        try:
            line = self.format(record)
            self._sink(record.levelname, line)
        except Exception:
            pass


# ============================================================
# LogPage
# ============================================================

class LogPage(QWidget):
    """实时日志 + 等级过滤"""

    _log_signal = pyqtSignal(str, str)        # (levelname, line)

    def __init__(self, max_lines: int = 2000):
        super().__init__()
        self._max_lines = max_lines
        self._show_levels = {
            "DEBUG": False,
            "INFO": True,
            "WARNING": True,
            "ERROR": True,
            "CRITICAL": True,
        }
        self._auto_scroll = True

        # 缓存所有日志（用于过滤切换时重画）
        self._all_lines: deque = deque(maxlen=max_lines)

        self._init_ui()
        self._log_signal.connect(self._on_log_emitted)

        # 注册到 root logger
        self._handler = _UiLogHandler(self._sink)
        self._handler.setLevel(logging.DEBUG)
        self._handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        logging.getLogger().addHandler(self._handler)

    # ─────────── UI 构建 ───────────

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── 工具栏 ──
        bar = QHBoxLayout()
        bar.addWidget(QLabel("过滤:"))

        self._cb_info = QCheckBox("INFO")
        self._cb_info.setChecked(True)
        self._cb_warn = QCheckBox("WARN")
        self._cb_warn.setChecked(True)
        self._cb_error = QCheckBox("ERROR")
        self._cb_error.setChecked(True)
        for cb in (self._cb_info, self._cb_warn, self._cb_error):
            cb.toggled.connect(self._on_filter_changed)
            cb.setStyleSheet(
                "QCheckBox { color: #c0c8d8; padding: 0 6px; }"
                "QCheckBox::indicator { width: 14px; height: 14px; }"
            )
            bar.addWidget(cb)

        bar.addStretch()

        self._cb_auto = QCheckBox("自动滚动")
        self._cb_auto.setChecked(True)
        self._cb_auto.toggled.connect(self._on_autoscroll_toggled)
        self._cb_auto.setStyleSheet(
            "QCheckBox { color: #c0c8d8; padding: 0 6px; }"
            "QCheckBox::indicator { width: 14px; height: 14px; }"
        )
        bar.addWidget(self._cb_auto)

        clear_btn = QPushButton("清屏")
        clear_btn.setFixedWidth(64)
        clear_btn.clicked.connect(self._clear)
        bar.addWidget(clear_btn)

        layout.addLayout(bar)

        # ── 文本框 ──
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(self._max_lines)
        # 等宽字体
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self._text.setFont(font)
        self._text.setStyleSheet(
            "QPlainTextEdit { background-color: #0d1b2a;"
            " color: #d0d0d0; border: 1px solid #0f3460;"
            " border-radius: 4px; padding: 4px; }"
        )
        layout.addWidget(self._text, 1)

    # ─────────── 信号槽 ───────────

    def _sink(self, level: str, line: str):
        """logging Handler 调用（任何线程） → 转 signal 到 UI 线程"""
        self._log_signal.emit(level, line)

    def _on_log_emitted(self, level: str, line: str):
        """UI 线程：缓存 + 显示（如果当前过滤允许）"""
        self._all_lines.append((level, line))
        if self._show_levels.get(level, True):
            self._append(level, line)

    def _append(self, level: str, line: str):
        color = LEVEL_COLOR.get(level, "#d0d0d0")
        # appendHtml 自动加换行
        safe = (line.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))
        self._text.appendHtml(
            f'<span style="color:{color}">{safe}</span>'
        )
        if self._auto_scroll:
            sb = self._text.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_filter_changed(self):
        self._show_levels["INFO"]     = self._cb_info.isChecked()
        self._show_levels["WARNING"]  = self._cb_warn.isChecked()
        self._show_levels["ERROR"]    = self._cb_error.isChecked()
        self._show_levels["CRITICAL"] = self._cb_error.isChecked()
        # 重画
        self._text.clear()
        for level, line in self._all_lines:
            if self._show_levels.get(level, True):
                self._append(level, line)

    def _on_autoscroll_toggled(self, on: bool):
        self._auto_scroll = on
        if on:
            sb = self._text.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _clear(self):
        self._text.clear()
        # 不清缓存 — 切换过滤还能恢复

    # ─────────── 生命周期 ───────────

    def detach(self):
        """从 root logger 摘掉 handler（避免应用关闭后还接日志）"""
        try:
            logging.getLogger().removeHandler(self._handler)
        except Exception:
            pass

    def closeEvent(self, event):
        self.detach()
        super().closeEvent(event)
