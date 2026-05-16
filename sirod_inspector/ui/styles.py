"""
全局 QSS 样式表 - 深色工业风主题
================================
匹配设计截图的视觉风格。
"""

DARK_STYLE = """
/* ═══════ 全局 ═══════ */
QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMainWindow { background-color: #1a1a2e; }

/* ═══════ TopBar ═══════ */
#TopBar {
    background-color: #16213e;
    border-bottom: 1px solid #0f3460;
    min-height: 46px; max-height: 46px;
}
#TopBar QLabel {
    background: transparent; border: none;
}
#TopBar QLabel#AppTitle {
    color: #e0e0e0; font-size: 16px; font-weight: bold;
}
#TopBar QPushButton {
    background: transparent; color: #a0a0b8; border: none;
    padding: 8px 18px; font-size: 13px; border-radius: 0px;
    border-bottom: 2px solid transparent;
}
#TopBar QPushButton:hover { color: #ffffff; }
#TopBar QPushButton:checked {
    color: #00d4ff; border-bottom: 2px solid #00d4ff;
}

/* ═══════ BottomBar ═══════ */
#BottomBar {
    background-color: #16213e; border-top: 1px solid #0f3460;
    min-height: 30px; max-height: 30px;
}
#BottomBar QLabel {
    font-size: 11px; color: #8888a0; padding: 0 8px;
    background: transparent; border: none;
}

/* ═══════ Table ═══════ */
QTableWidget {
    background-color: #16213e; alternate-background-color: #1a1a2e;
    border: 1px solid #0f3460; gridline-color: #0f3460;
    selection-background-color: rgba(0, 212, 255, 0.15);
}
QTableWidget::item { padding: 6px 10px; border-bottom: 1px solid #0f3460; }
QHeaderView::section {
    background-color: #0f3460; color: #c0c0d0; font-weight: bold;
    padding: 6px 10px; border: none; border-right: 1px solid #16213e;
}

/* ═══════ Buttons ═══════ */
QPushButton {
    background-color: #0f3460; color: #e0e0e0; border: 1px solid #1a5276;
    border-radius: 4px; padding: 6px 16px; font-size: 12px;
}
QPushButton:hover { background-color: #1a5276; color: #ffffff; }
QPushButton:pressed { background-color: #0d2b4a; }
QPushButton:disabled { background-color: #2a2a3e; color: #555566; }
QPushButton#PrimaryBtn {
    background-color: #00d4ff; color: #1a1a2e; font-weight: bold; border: none;
}
QPushButton#PrimaryBtn:hover { background-color: #33ddff; }
QPushButton#DangerBtn { background-color: #e74c3c; color: #ffffff; border: none; }

/* ═══════ Input ═══════ */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #0d1b2a; color: #e0e0e0;
    border: 1px solid #0f3460; border-radius: 4px; padding: 5px 10px;
}
QLineEdit:focus, QSpinBox:focus { border-color: #00d4ff; }
QComboBox QAbstractItemView {
    background-color: #16213e; color: #e0e0e0; selection-background-color: #0f3460;
}

/* ═══════ ScrollBar ═══════ */
QScrollBar:vertical { background: #1a1a2e; width: 8px; border: none; }
QScrollBar::handle:vertical { background: #0f3460; min-height: 30px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

/* ═══════ GroupBox ═══════ */
QGroupBox {
    border: 1px solid #0f3460; border-radius: 6px;
    margin-top: 12px; padding-top: 18px; font-weight: bold; color: #00d4ff;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }

/* ═══════ ScrollArea ═══════ */
QScrollArea { border: none; background: transparent; }
"""
