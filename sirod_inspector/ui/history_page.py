"""
历史记录页面 - 搜索/筛选栏 + 数据表格 + 分页 + 导出Excel
"""
import logging
import datetime
import json

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QComboBox, QDateEdit,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QFileDialog, QMessageBox,
    QDialog, QDialogButtonBox,
)

logger = logging.getLogger(__name__)


class HistoryPage(QWidget):
    def __init__(self, database=None):
        super().__init__()
        self._db = database
        self._page = 1
        self._page_size = 20
        self._total = 0
        self._records = []
        self._init_ui()

    def set_database(self, db):
        self._db = db

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 搜索/筛选栏
        filter_frame = QFrame()
        filter_frame.setStyleSheet(
            "QFrame { background-color: #16213e; border: 1px solid #0f3460; "
            "border-radius: 8px; padding: 8px; }"
        )
        fl = QHBoxLayout(filter_frame)
        fl.setSpacing(10)

        fl.addWidget(QLabel("晶棒编号:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("输入编号搜索...")
        self._search_input.setFixedWidth(160)
        fl.addWidget(self._search_input)

        fl.addWidget(QLabel("结果:"))
        self._result_combo = QComboBox()
        self._result_combo.addItems(["全部", "OK", "NG"])
        self._result_combo.setFixedWidth(80)
        fl.addWidget(self._result_combo)

        fl.addWidget(QLabel("起始日期:"))
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDate(self._date_from.date().currentDate().addDays(-30))
        self._date_from.setFixedWidth(130)
        fl.addWidget(self._date_from)

        fl.addWidget(QLabel("结束日期:"))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDate(self._date_to.date().currentDate())
        self._date_to.setFixedWidth(130)
        fl.addWidget(self._date_to)

        search_btn = QPushButton("查询")
        search_btn.setObjectName("PrimaryBtn")
        search_btn.clicked.connect(self._do_search)
        fl.addWidget(search_btn)

        fl.addStretch()

        export_btn = QPushButton("导出 Excel")
        export_btn.clicked.connect(self._export_excel)
        fl.addWidget(export_btn)

        layout.addWidget(filter_frame)

        # 数据表格
        self._table = QTableWidget()
        self._table.setColumnCount(8)
        self._table.setHorizontalHeaderLabels([
            "ID", "晶棒编号", "检测时间", "结果", "缺陷类型",
            "缺陷数量", "耗时(ms)", "产线"
        ])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.cellDoubleClicked.connect(self._show_defect_detail)
        layout.addWidget(self._table, 1)

        # 分页栏
        page_frame = QFrame()
        pl = QHBoxLayout(page_frame)
        pl.setContentsMargins(0, 4, 0, 4)

        self._page_info = QLabel("第 1 页 / 共 0 页 (共 0 条)")
        self._page_info.setStyleSheet("color: #8888a0;")
        pl.addWidget(self._page_info)
        pl.addStretch()

        self._prev_btn = QPushButton("上一页")
        self._prev_btn.clicked.connect(self._prev_page)
        pl.addWidget(self._prev_btn)

        self._next_btn = QPushButton("下一页")
        self._next_btn.clicked.connect(self._next_page)
        pl.addWidget(self._next_btn)

        layout.addWidget(page_frame)

    def _do_search(self):
        self._page = 1
        self._load_data()

    def _load_data(self):
        """★ 异步 ★ — DB 查询扔到 QThreadPool，UI 不卡。"""
        if not self._db or not self._db.is_connected:
            return
        if getattr(self, "_loading", False):
            return  # 上一次还没回来
        self._loading = True

        rod_id = self._search_input.text().strip() or None
        result = self._result_combo.currentText()
        date_from = self._date_from.date().toString("yyyy-MM-dd") + " 00:00:00"
        date_to = self._date_to.date().toString("yyyy-MM-dd") + " 23:59:59"

        # 临时提示加载中
        self._page_info.setText("加载中...")

        from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

        class _Signals(QObject):
            done = pyqtSignal(object, int)  # (records, total)
            error = pyqtSignal(str)

        class _Task(QRunnable):
            def __init__(self, db, **kw):
                super().__init__()
                self.db = db
                self.kw = kw
                self.signals = _Signals()
            def run(self):
                try:
                    records, total = self.db.query_records(**self.kw)
                    self.signals.done.emit(records, total)
                except Exception as e:
                    self.signals.error.emit(f"{type(e).__name__}: {e}")

        task = _Task(
            self._db,
            date_from=date_from, date_to=date_to,
            rod_id=rod_id, result=result,
            page=self._page, page_size=self._page_size,
        )
        task.signals.done.connect(self._on_load_done)
        task.signals.error.connect(self._on_load_error)
        QThreadPool.globalInstance().start(task)

    def _on_load_done(self, records, total):
        """后台 DB 查完，回主线程刷表格"""
        self._loading = False
        self._records = records
        self._total = total
        total_pages = max(1, (total + self._page_size - 1) // self._page_size)
        self._page_info.setText(f"第 {self._page} 页 / 共 {total_pages} 页 (共 {total} 条)")

        self._table.setRowCount(0)
        for rec in records:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(str(rec.get("id", ""))))
            self._table.setItem(row, 1, QTableWidgetItem(rec.get("rod_id", "")))
            t = rec.get("inspect_time", "")
            if hasattr(t, "strftime"):
                t = t.strftime("%Y-%m-%d %H:%M:%S")
            self._table.setItem(row, 2, QTableWidgetItem(str(t)))
            result_item = QTableWidgetItem(rec.get("result", ""))
            if rec.get("result") == "NG":
                result_item.setForeground(Qt.GlobalColor.red)
            else:
                result_item.setForeground(Qt.GlobalColor.green)
            self._table.setItem(row, 3, result_item)
            self._table.setItem(row, 4, QTableWidgetItem(rec.get("defect_type", "-") or "-"))
            self._table.setItem(row, 5, QTableWidgetItem(str(rec.get("defect_count", 0))))
            self._table.setItem(row, 6, QTableWidgetItem(str(rec.get("duration_ms", 0))))
            self._table.setItem(row, 7, QTableWidgetItem(rec.get("line_id", "")))

    def _on_load_error(self, err):
        self._loading = False
        self._page_info.setText(f"加载失败: {err}")

    def _show_defect_detail(self, row, _col):
        """双击某根棒 → 弹窗显示该棒所有缺陷明细（类别/置信度/面积/长度，px + mm）。"""
        if row < 0 or row >= len(self._records):
            return
        rec = self._records[row]
        raw = rec.get("DefectsJSON") or rec.get("defects_json") or ""
        rod = rec.get("rod_id", "") or rec.get("SquareNumber", "")
        try:
            defects = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            defects = []

        dlg = QDialog(self)
        dlg.setWindowTitle(f"缺陷明细 — {rod}")
        dlg.resize(720, 320)
        v = QVBoxLayout(dlg)

        if not defects:
            v.addWidget(QLabel(
                "该记录无缺陷明细。\n"
                "（OK 且无缺陷，或为此功能上线前检测的旧数据）"
            ))
        else:
            def _fmt(x, nd=2):
                try:
                    return f"{float(x):.{nd}f}"
                except (ValueError, TypeError):
                    return "-"

            headers = ["#", "类别", "置信度", "面积(px²)", "面积(mm²)",
                       "长度(px·半径)", "长度(mm·直径)"]
            tbl = QTableWidget()
            tbl.setColumnCount(len(headers))
            tbl.setHorizontalHeaderLabels(headers)
            tbl.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.Stretch)
            tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            tbl.verticalHeader().setVisible(False)
            tbl.setRowCount(len(defects))
            for i, d in enumerate(defects):
                cells = [
                    str(i + 1),
                    str(d.get("class_name") or "未分类"),
                    _fmt(d.get("class_confidence"), 3),
                    _fmt(d.get("area"), 0),
                    _fmt(d.get("area_mm2"), 2),
                    _fmt(d.get("outer_radius"), 2),
                    _fmt(d.get("length_mm"), 2),
                ]
                for c, text in enumerate(cells):
                    tbl.setItem(i, c, QTableWidgetItem(text))
            v.addWidget(tbl)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        dlg.exec()

    def _prev_page(self):
        if self._page > 1:
            self._page -= 1
            self._load_data()

    def _next_page(self):
        total_pages = max(1, (self._total + self._page_size - 1) // self._page_size)
        if self._page < total_pages:
            self._page += 1
            self._load_data()

    def _export_excel(self):
        try:
            import openpyxl
        except ImportError:
            QMessageBox.warning(self, "提示", "请先安装 openpyxl: pip install openpyxl")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Excel", f"检测记录_{datetime.date.today()}.xlsx",
            "Excel Files (*.xlsx)"
        )
        if not path:
            return
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "检测记录"
            headers = [self._table.horizontalHeaderItem(c).text()
                       for c in range(self._table.columnCount())]
            ws.append(headers)
            for row in range(self._table.rowCount()):
                ws.append([
                    (self._table.item(row, c).text() if self._table.item(row, c) else "")
                    for c in range(self._table.columnCount())
                ])
            wb.save(path)
            QMessageBox.information(self, "成功", f"已导出到: {path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"导出失败: {e}")

    def refresh(self):
        self._load_data()
