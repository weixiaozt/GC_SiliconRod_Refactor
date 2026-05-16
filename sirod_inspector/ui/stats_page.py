"""
统计报表页面 - 汇总卡片 + matplotlib图表
"""
import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QDateEdit, QFrame, QSizePolicy,
)

logger = logging.getLogger(__name__)

try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from matplotlib.figure import Figure
    import matplotlib
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


class StatsPage(QWidget):
    def __init__(self, database=None):
        super().__init__()
        self._db = database
        self._init_ui()

    def set_database(self, db):
        self._db = db

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_frame = QFrame()
        top_frame.setStyleSheet(
            "QFrame { background-color: #16213e; border: 1px solid #0f3460; "
            "border-radius: 8px; padding: 8px; }"
        )
        tl = QHBoxLayout(top_frame)
        tl.setSpacing(10)

        tl.addWidget(QLabel("统计日期:"))
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDate(self._date_from.date().currentDate().addDays(-7))
        self._date_from.setFixedWidth(130)
        tl.addWidget(self._date_from)

        tl.addWidget(QLabel("至"))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDate(self._date_to.date().currentDate())
        self._date_to.setFixedWidth(130)
        tl.addWidget(self._date_to)

        refresh_btn = QPushButton("刷新统计")
        refresh_btn.setObjectName("PrimaryBtn")
        refresh_btn.clicked.connect(self.refresh)
        tl.addWidget(refresh_btn)
        tl.addStretch()

        layout.addWidget(top_frame)

        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(10)

        self._lbl_total = self._make_stat_card("检测总数", "0", "#00d4ff")
        self._lbl_ok = self._make_stat_card("合格数", "0", "#27ae60")
        self._lbl_ng = self._make_stat_card("缺陷数", "0", "#e74c3c")
        self._lbl_rate = self._make_stat_card("合格率", "0%", "#f39c12")

        for card, _ in [self._lbl_total, self._lbl_ok, self._lbl_ng, self._lbl_rate]:
            cards_layout.addWidget(card)
        layout.addLayout(cards_layout)

        if HAS_MPL:
            charts_layout = QHBoxLayout()
            charts_layout.setSpacing(10)

            self._fig_pie = Figure(figsize=(4, 3), facecolor="#1a1a2e")
            self._canvas_pie = FigureCanvasQTAgg(self._fig_pie)
            self._canvas_pie.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            charts_layout.addWidget(self._canvas_pie)

            self._fig_bar = Figure(figsize=(6, 3), facecolor="#1a1a2e")
            self._canvas_bar = FigureCanvasQTAgg(self._fig_bar)
            self._canvas_bar.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            charts_layout.addWidget(self._canvas_bar)

            layout.addLayout(charts_layout, 1)
        else:
            placeholder = QLabel("请安装 matplotlib 以显示统计图表")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #555566; font-size: 16px;")
            layout.addWidget(placeholder, 1)

    def _make_stat_card(self, label, value, color):
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { background-color: #16213e; border: 1px solid #0f3460; "
            "border-radius: 8px; padding: 10px; }"
        )
        vl = QVBoxLayout(frame)
        vl.setContentsMargins(12, 8, 12, 8)
        val_lbl = QLabel(value)
        val_lbl.setStyleSheet(f"font-size: 28px; font-weight: bold; color: {color};")
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(val_lbl)
        name_lbl = QLabel(label)
        name_lbl.setStyleSheet("font-size: 12px; color: #8888a0;")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(name_lbl)
        return frame, val_lbl

    def refresh(self):
        if not self._db or not self._db.is_connected:
            return
        date_from = self._date_from.date().toString("yyyy-MM-dd") + " 00:00:00"
        date_to = self._date_to.date().toString("yyyy-MM-dd") + " 23:59:59"
        stats = self._db.get_stats(date_from, date_to)
        self._lbl_total[1].setText(str(stats["total"]))
        self._lbl_ok[1].setText(str(stats["ok"]))
        self._lbl_ng[1].setText(str(stats["ng"]))
        self._lbl_rate[1].setText(f"{stats['pass_rate']}%")
        if HAS_MPL:
            self._draw_pie(stats["ok"], stats["ng"])
            self._draw_bar()

    def _draw_pie(self, ok, ng):
        self._fig_pie.clear()
        ax = self._fig_pie.add_subplot(111)
        ax.set_facecolor("#1a1a2e")
        if ok + ng == 0:
            ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                    color="#555566", fontsize=14, transform=ax.transAxes)
        else:
            ax.pie([ok, ng], labels=["合格", "缺陷"], colors=["#27ae60", "#e74c3c"],
                   autopct="%1.1f%%", textprops={"color": "#e0e0e0", "fontsize": 11})
            ax.set_title("合格率分布", color="#00d4ff", fontsize=13)
        self._canvas_pie.draw()

    def _draw_bar(self):
        self._fig_bar.clear()
        ax = self._fig_bar.add_subplot(111)
        ax.set_facecolor("#1a1a2e")
        date_str = self._date_to.date().toString("yyyy-MM-dd")
        hourly = self._db.get_hourly_stats(date_str)
        if not hourly:
            ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                    color="#555566", fontsize=14, transform=ax.transAxes)
        else:
            import numpy as np
            hours_ok, hours_ng = {}, {}
            for row in hourly:
                h = row["hour"]
                if row["result"] == "OK":
                    hours_ok[h] = row["cnt"]
                else:
                    hours_ng[h] = row["cnt"]
            all_hours = sorted(set(list(hours_ok.keys()) + list(hours_ng.keys())))
            ok_vals = [hours_ok.get(h, 0) for h in all_hours]
            ng_vals = [hours_ng.get(h, 0) for h in all_hours]
            x = np.arange(len(all_hours))
            width = 0.35
            ax.bar(x - width / 2, ok_vals, width, label="合格", color="#27ae60")
            ax.bar(x + width / 2, ng_vals, width, label="缺陷", color="#e74c3c")
            ax.set_xticks(x)
            ax.set_xticklabels([f"{h}:00" for h in all_hours], rotation=45,
                               fontsize=9, color="#8888a0")
            ax.tick_params(axis="y", colors="#8888a0")
            ax.legend(facecolor="#16213e", edgecolor="#0f3460", labelcolor="#e0e0e0")
            ax.set_title(f"{date_str} 每小时检测量", color="#00d4ff", fontsize=13)
        self._fig_bar.tight_layout()
        self._canvas_bar.draw()
