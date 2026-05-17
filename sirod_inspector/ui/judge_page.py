"""
判定参数设置页（顶部「参数」tab）
=================================
分两块：

  ① 全局几何阈值（决定哪些 seg 候选送分类） —— 单缺陷面积/长度/总面积/个数
  ② 按类别独立规则 —— 每类一行 ×  5 个字段:
       计入 NG / 最大面积 / 最大长度 / 最大数量 / 最低置信度

  ┌─类别─┬─NG──┬─maxArea─┬─maxLen─┬─maxCount─┬─minConf─┐
  │ 隐裂 │  ✓  │   10    │   2    │   10     │  0.50   │
  │ 崩边 │  ✓  │  100    │   5    │   10     │  0.50   │
  │ 脏污 │     │   --    │   --   │   --     │   --    │
  │ ...                                                  │
  └──────┴─────┴─────────┴────────┴──────────┴─────────┘

保存后写入 ``config.json``::

    "judge": {
      "max_area": 10, "sum_area": 10, "max_count": 10, "max_length": 2,
      "per_class": [
        {"name": "隐裂", "report_ng": true,  "max_area": 10, "max_length": 2,
                                              "max_count": 10, "min_confidence": 0.5},
        {"name": "崩边", "report_ng": true,  ...},
        ...
      ]
    }

⚠ 改完需重启程序生效（Pipeline 在初始化时加载规则）。
"""

from __future__ import annotations

from typing import List

from PyQt6.QtCore import Qt, pyqtSignal, QLocale
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QGroupBox, QFormLayout, QPushButton,
    QSpinBox, QDoubleSpinBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QScrollArea,
)


# 默认覆盖的所有 cls 类别（cls 模型固定 7 类 + 缺口）
_DEFAULT_CLASSES = ["隐裂", "崩边", "其他", "脏污", "线痕", "拼缝", "OK", "缺口"]


_C_LOCALE = QLocale(QLocale.Language.C)


def _style_spin(sp):
    """统一表格内 spin box 显示。

    两个问题一起修：
    1. 之前用 Windows 中文 locale，小数分隔符是全角"．" / "。"，看起来像
       空格（用户截图："10.0" 显示成 "10 0"，"100.0" 显示成 "100 0"）。
       → 强制 C locale，用 ASCII "."
    2. 上下箭头默认布局会挤压显示区，给最小宽 110 + padding 留位置。
    """
    sp.setLocale(_C_LOCALE)
    sp.setMinimumWidth(110)
    sp.setAlignment(Qt.AlignmentFlag.AlignCenter)
    sp.setStyleSheet(
        "QAbstractSpinBox { padding-right: 18px; padding-left: 4px; }"
    )


class JudgePage(QWidget):
    """参数设置页"""

    # 保存按钮触发的信号 — main_camera 收到后会提示重启
    settings_saved = pyqtSignal()

    def __init__(self, config=None):
        super().__init__()
        self._config = config
        self._init_ui()
        if config:
            self._load_from_config()

    def set_config(self, config):
        self._config = config
        self._load_from_config()

    # ─────────── UI 构建 ───────────

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(16)

        # ── 全局几何阈值 ──
        global_group = QGroupBox("全局几何阈值（决定哪些 seg 候选送分类）")
        gf = QFormLayout(global_group)

        self._sp_max_area = QDoubleSpinBox()
        self._sp_max_area.setRange(0, 1e9)
        self._sp_max_area.setValue(10)
        self._sp_max_area.setDecimals(1)
        self._sp_max_area.setLocale(_C_LOCALE)
        self._sp_max_area.setToolTip(
            "单个缺陷面积上限（像素）。超过即进入分类阶段。")
        gf.addRow("max_area (单缺陷面积):", self._sp_max_area)

        self._sp_sum_area = QDoubleSpinBox()
        self._sp_sum_area.setRange(0, 1e9)
        self._sp_sum_area.setValue(10)
        self._sp_sum_area.setDecimals(1)
        self._sp_sum_area.setLocale(_C_LOCALE)
        self._sp_sum_area.setToolTip("所有缺陷总面积上限。")
        gf.addRow("sum_area (总面积):", self._sp_sum_area)

        self._sp_max_count = QSpinBox()
        self._sp_max_count.setRange(0, 1_000_000)
        self._sp_max_count.setValue(10)
        self._sp_max_count.setLocale(_C_LOCALE)
        self._sp_max_count.setToolTip("缺陷总数上限。")
        gf.addRow("max_count (总数量):", self._sp_max_count)

        self._sp_max_length = QDoubleSpinBox()
        self._sp_max_length.setRange(0, 1e9)
        self._sp_max_length.setValue(2)
        self._sp_max_length.setDecimals(2)
        self._sp_max_length.setLocale(_C_LOCALE)
        self._sp_max_length.setToolTip("单个缺陷 outer_radius（外接圆半径）上限。")
        gf.addRow("max_length (单缺陷长度):", self._sp_max_length)

        layout.addWidget(global_group)

        # ── 按类别规则 ──
        per_class_group = QGroupBox("按类别判定规则（最终是否报 NG）")
        pc_layout = QVBoxLayout(per_class_group)

        hint = QLabel(
            "勾选「NG」列才会把该类的缺陷计入 NG。\n"
            "面积/长度超过该类阈值，且置信度 ≥ 最低置信度 → 报 NG。\n"
            "「最大数量」是该类在一根棒上累计数量上限（超即 NG）。"
        )
        hint.setStyleSheet(
            "color: #8888a0; font-size: 11px; background: transparent; border: none;"
        )
        hint.setWordWrap(True)
        pc_layout.addWidget(hint)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["类别", "NG", "最大面积", "最大长度", "最大数量", "最低置信度"]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 100)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(1, 60)
        # 行高 36 — 默认 ~25 会把 spin box 数字的底半截（含小数点）剪掉，
        # 用户截图里"100.0"显示成"100 0"就是这个原因
        self._table.verticalHeader().setDefaultSectionSize(36)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setStyleSheet(
            "QTableWidget { background-color: #0d1b2a; border: 1px solid #0f3460;"
            " color: #e0e0e0; gridline-color: #0f3460; }"
            "QTableWidget::item { padding: 4px 6px; }"
            "QHeaderView::section { background-color: #16213e; color: #e0e0e0;"
            " border: none; border-bottom: 1px solid #0f3460; padding: 6px; }"
        )
        self._table.setMinimumHeight(280)
        pc_layout.addWidget(self._table)

        layout.addWidget(per_class_group)

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        # ── 底部按钮 ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self._load_from_config)
        btn_layout.addWidget(cancel_btn)
        save_btn = QPushButton("保存设置")
        save_btn.setObjectName("PrimaryBtn")
        save_btn.clicked.connect(self._save_settings)
        btn_layout.addWidget(save_btn)
        outer.addLayout(btn_layout)

    # ─────────── 表格行管理 ───────────

    def _populate_table(self, rules_data: list):
        """rules_data: list[dict]，每项 含 name/report_ng/max_area/...

        rules_data 长度 / 顺序决定表格行。"""
        self._table.setRowCount(0)
        for row_data in rules_data:
            self._add_row(row_data)

    def _add_row(self, row_data: dict):
        r = self._table.rowCount()
        self._table.insertRow(r)

        # 0: 类别名（不可编辑的 item）
        name_item = QTableWidgetItem(str(row_data.get("name", "")))
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(r, 0, name_item)

        # 1: NG checkbox
        cb = QCheckBox()
        cb.setChecked(bool(row_data.get("report_ng", False)))
        cb_holder = QWidget()
        cb_l = QHBoxLayout(cb_holder)
        cb_l.setContentsMargins(0, 0, 0, 0)
        cb_l.addStretch(); cb_l.addWidget(cb); cb_l.addStretch()
        self._table.setCellWidget(r, 1, cb_holder)

        # 2: max_area
        sp_ma = QDoubleSpinBox()
        sp_ma.setRange(0, 1e9)
        sp_ma.setDecimals(1)
        sp_ma.setValue(float(row_data.get("max_area", 1e6)))
        _style_spin(sp_ma)
        self._table.setCellWidget(r, 2, sp_ma)

        # 3: max_length
        sp_ml = QDoubleSpinBox()
        sp_ml.setRange(0, 1e9)
        sp_ml.setDecimals(2)
        sp_ml.setValue(float(row_data.get("max_length", 1e6)))
        _style_spin(sp_ml)
        self._table.setCellWidget(r, 3, sp_ml)

        # 4: max_count
        sp_mc = QSpinBox()
        sp_mc.setRange(0, 1_000_000)
        sp_mc.setValue(int(row_data.get("max_count", 1000)))
        _style_spin(sp_mc)
        self._table.setCellWidget(r, 4, sp_mc)

        # 5: min_confidence
        sp_conf = QDoubleSpinBox()
        sp_conf.setRange(0.0, 1.0)
        sp_conf.setDecimals(2)
        sp_conf.setSingleStep(0.05)
        sp_conf.setValue(float(row_data.get("min_confidence", 0.0)))
        _style_spin(sp_conf)
        self._table.setCellWidget(r, 5, sp_conf)

    def _read_table(self) -> List[dict]:
        rules = []
        for r in range(self._table.rowCount()):
            name_item = self._table.item(r, 0)
            if not name_item or not name_item.text().strip():
                continue
            cb_holder = self._table.cellWidget(r, 1)
            cb = cb_holder.findChild(QCheckBox) if cb_holder else None
            rules.append({
                "name":           name_item.text().strip(),
                "report_ng":      bool(cb.isChecked()) if cb else False,
                "max_area":       float(self._table.cellWidget(r, 2).value()),
                "max_length":     float(self._table.cellWidget(r, 3).value()),
                "max_count":      int(self._table.cellWidget(r, 4).value()),
                "min_confidence": float(self._table.cellWidget(r, 5).value()),
            })
        return rules

    # ─────────── config 读写 ───────────

    def _load_from_config(self):
        if not self._config:
            return

        # 全局阈值
        self._sp_max_area.setValue(
            float(self._config.get("judge.max_area", 10)))
        self._sp_sum_area.setValue(
            float(self._config.get("judge.sum_area", 10)))
        self._sp_max_count.setValue(
            int(self._config.get("judge.max_count", 10)))
        self._sp_max_length.setValue(
            float(self._config.get("judge.max_length", 2)))

        # 每类规则
        per_class = self._config.get("judge.per_class", None)
        rules_data: list[dict] = []

        if isinstance(per_class, list) and per_class:
            # 用户已配置过 per_class — 直接用
            rules_data = [dict(d) for d in per_class if isinstance(d, dict)]
            seen = {d.get("name", "") for d in rules_data}
            for cls in _DEFAULT_CLASSES:
                if cls not in seen:
                    rules_data.append(self._default_rule_for(cls))
        else:
            # 没配 per_class — 兼容老 config 的 ng_trigger_classes
            ng_classes_cfg = self._config.get(
                "judge.ng_trigger_classes", None)
            ng_set = (set(ng_classes_cfg)
                      if isinstance(ng_classes_cfg, list) and ng_classes_cfg
                      else {"隐裂"})
            for cls in _DEFAULT_CLASSES:
                d = self._default_rule_for(cls)
                # 按 ng_trigger_classes 决定该类是否报 NG（升级用户看到的就是
                # Pipeline 实际生效的配置，不会跟检测行为对不上）
                d["report_ng"] = (cls in ng_set)
                rules_data.append(d)

        self._populate_table(rules_data)

    @staticmethod
    def _default_rule_for(name: str) -> dict:
        # 跟 algorithm/judge.DEFAULT_CLASS_RULES 对齐（min_confidence=0.0 = 不过滤，
        # 等价 Halcon 原行为；用户想加 conf 过滤可在 UI 调高）
        if name == "隐裂":
            return {"name": name, "report_ng": True,
                    "max_area": 10.0, "max_length": 2.0,
                    "max_count": 10, "min_confidence": 0.0}
        # 其它类默认不报 NG（即使配了阈值也不生效，直到用户勾上 NG）
        return {"name": name, "report_ng": False,
                "max_area": 100.0, "max_length": 5.0,
                "max_count": 10, "min_confidence": 0.0}

    def _save_settings(self):
        if not self._config:
            QMessageBox.warning(self, "提示", "配置对象未初始化")
            return

        self._config.set("judge.max_area",   self._sp_max_area.value())
        self._config.set("judge.sum_area",   self._sp_sum_area.value())
        self._config.set("judge.max_count",  self._sp_max_count.value())
        self._config.set("judge.max_length", self._sp_max_length.value())
        rules = self._read_table()
        self._config.set("judge.per_class",  rules)
        # 兼容字段 — 给老代码用
        ng_classes = [d["name"] for d in rules if d["report_ng"]]
        self._config.set("judge.ng_trigger_classes", ng_classes)
        self._config.save()

        self.settings_saved.emit()
        QMessageBox.information(
            self, "已保存",
            "判定参数已保存，下一棒检测立即生效。"
        )
