"""
系统设置页面 - 分组配置项，支持保存/取消
包含缺陷类型管理功能
"""
import logging

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QSpinBox, QCheckBox,
    QGroupBox, QFormLayout, QScrollArea,
    QMessageBox, QTimeEdit, QListWidget, QListWidgetItem,
    QInputDialog, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QDoubleSpinBox,
)
from PyQt6.QtCore import QTime, pyqtSignal

logger = logging.getLogger(__name__)


class SettingsPage(QWidget):
    """系统设置页面"""

    # 缺陷类型变更信号（通知 GalleryPage 更新下拉列表）
    defect_types_changed = pyqtSignal(list)

    # 串口设置变更信号（通知 main.py 重新打开串口）
    serial_settings_changed = pyqtSignal()

    # HTTP MES 上传设置变更信号（通知 main.py 可选刷新，目前主要是让配置立即生效）
    http_settings_changed = pyqtSignal()

    def __init__(self, config=None):
        super().__init__()
        self._config = config
        self._widgets = {}
        self._init_ui()
        if config:
            self._load_from_config()

    def set_config(self, config):
        self._config = config
        self._load_from_config()

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(16)

        # ── TCP 通信设置 ──
        tcp_group = QGroupBox("TCP 通信设置")
        tcp_form = QFormLayout(tcp_group)
        tcp_form.setSpacing(8)
        self._widgets["tcp.host"] = QLineEdit("127.0.0.1")
        tcp_form.addRow("服务器 IP:", self._widgets["tcp.host"])
        self._widgets["tcp.port"] = QSpinBox()
        self._widgets["tcp.port"].setRange(1, 65535)
        self._widgets["tcp.port"].setValue(3000)
        tcp_form.addRow("端口号:", self._widgets["tcp.port"])
        layout.addWidget(tcp_group)

        # ── MySQL 数据库设置 ──
        db_group = QGroupBox("MySQL 数据库设置")
        db_form = QFormLayout(db_group)
        db_form.setSpacing(8)
        self._widgets["database.host"] = QLineEdit("127.0.0.1")
        db_form.addRow("数据库 IP:", self._widgets["database.host"])
        self._widgets["database.port"] = QSpinBox()
        self._widgets["database.port"].setRange(1, 65535)
        self._widgets["database.port"].setValue(3306)
        db_form.addRow("端口号:", self._widgets["database.port"])
        self._widgets["database.user"] = QLineEdit("root")
        db_form.addRow("用户名:", self._widgets["database.user"])
        self._widgets["database.password"] = QLineEdit("123456")
        self._widgets["database.password"].setEchoMode(QLineEdit.EchoMode.Password)
        db_form.addRow("密码:", self._widgets["database.password"])
        self._widgets["database.database"] = QLineEdit("b_xmartsql")
        db_form.addRow("数据库名:", self._widgets["database.database"])
        self._widgets["database.table"] = QLineEdit("squarstickresult")
        db_form.addRow("数据表名:", self._widgets["database.table"])
        layout.addWidget(db_group)

        # ── 班次清零设置 ──
        shift_group = QGroupBox("班次清零设置")
        shift_form = QFormLayout(shift_group)
        shift_form.setSpacing(8)

        shift_desc = QLabel(
            "统计数据（检测数量/合格数量/NG数量）将在以下时间点自动清零。\n"
            "软件重启后统计数据不会丢失，会继承上一次的数量。"
        )
        shift_desc.setStyleSheet(
            "color: #8888a0; font-size: 11px; background: transparent; border: none;"
        )
        shift_desc.setWordWrap(True)
        shift_form.addRow(shift_desc)

        self._shift_time_1 = QTimeEdit()
        self._shift_time_1.setDisplayFormat("HH:mm")
        self._shift_time_1.setTime(QTime(8, 0))
        shift_form.addRow("白班清零时间:", self._shift_time_1)

        self._shift_time_2 = QTimeEdit()
        self._shift_time_2.setDisplayFormat("HH:mm")
        self._shift_time_2.setTime(QTime(20, 0))
        shift_form.addRow("夜班清零时间:", self._shift_time_2)

        layout.addWidget(shift_group)

        # ── 缺陷类型管理 ──
        defect_group = QGroupBox("缺陷类型管理")
        defect_layout = QVBoxLayout(defect_group)
        defect_layout.setSpacing(8)

        defect_desc = QLabel(
            "\u7ba1\u7406\u7f3a\u9677\u7c7b\u578b\u5217\u8868\u3002Halcon \u7aef\u4f20\u8fc7\u6765\u7684\u300c\u7c7b\u578b\u300d\u5b57\u6bb5\u503c\u5e94\u4e0e\u6b64\u5217\u8868\u4e2d\u7684\u540d\u79f0\u4e00\u81f4\u3002\n"
            "\u7f3a\u9677\u56fe\u5e93\u548c\u56fe\u50cf\u5b58\u50a8\u5c06\u6309\u8fd9\u4e9b\u7c7b\u578b\u5206\u7c7b\u3002"
        )
        defect_desc.setStyleSheet(
            "color: #8888a0; font-size: 11px; background: transparent; border: none;"
        )
        defect_desc.setWordWrap(True)
        defect_layout.addWidget(defect_desc)

        # 缺陷类型列表
        self._defect_type_list = QListWidget()
        self._defect_type_list.setMaximumHeight(120)
        self._defect_type_list.setStyleSheet(
            "QListWidget { background-color: #0d1b2a; border: 1px solid #0f3460; "
            "border-radius: 4px; color: #e0e0e0; }"
            "QListWidget::item { padding: 4px 8px; }"
            "QListWidget::item:selected { background-color: #0f3460; }"
        )
        defect_layout.addWidget(self._defect_type_list)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        add_btn = QPushButton("添加类型")
        add_btn.clicked.connect(self._add_defect_type)
        btn_row.addWidget(add_btn)

        remove_btn = QPushButton("删除选中")
        remove_btn.clicked.connect(self._remove_defect_type)
        btn_row.addWidget(remove_btn)

        btn_row.addStretch()
        defect_layout.addLayout(btn_row)

        layout.addWidget(defect_group)

        # ── 飞书同步设置 ──
        feishu_group = QGroupBox("飞书同步设置")
        feishu_form = QFormLayout(feishu_group)
        feishu_form.setSpacing(8)

        # 启用开关
        self._widgets["feishu.enabled"] = QCheckBox("启用飞书同步")
        feishu_form.addRow("", self._widgets["feishu.enabled"])

        # 说明文字
        feishu_hint = QLabel(
            "App ID / App Secret: 飞书开放平台应用详情页获取"
            "App Token: 地址栏 /base/[这里]"
            "Table ID: 地址栏 ?table=[这里]（tbl 开头）"
        )
        feishu_hint.setStyleSheet(
            "color: #8888a0; font-size: 11px; background: transparent; border: none;"
        )
        feishu_hint.setWordWrap(True)
        feishu_form.addRow(feishu_hint)

        # App ID
        self._widgets["feishu.app_id"] = QLineEdit()
        self._widgets["feishu.app_id"].setPlaceholderText("cli_xxxxxxxxxxxxxxxx")
        feishu_form.addRow("App ID:", self._widgets["feishu.app_id"])

        # App Secret
        self._widgets["feishu.app_secret"] = QLineEdit()
        self._widgets["feishu.app_secret"].setEchoMode(QLineEdit.EchoMode.Password)
        self._widgets["feishu.app_secret"].setPlaceholderText("飞书应用 App Secret")
        feishu_form.addRow("App Secret:", self._widgets["feishu.app_secret"])

        # App Token（多维表格级别）
        self._widgets["feishu.app_token"] = QLineEdit()
        self._widgets["feishu.app_token"].setPlaceholderText(
            "多维表格 App Token（地址栏 /base/[这里]，如 MaBcXXXXXXXX）"
        )
        feishu_form.addRow("App Token:", self._widgets["feishu.app_token"])

        # Table ID（具体表格）
        self._widgets["feishu.table_id"] = QLineEdit()
        self._widgets["feishu.table_id"].setPlaceholderText(
            "具体表格 Table ID（地址栏 ?table=[这里]，tbl 开头）"
        )
        feishu_form.addRow("Table ID:", self._widgets["feishu.table_id"])

        # Base URL（高级，一般不需要改）
        self._widgets["feishu.base_url"] = QLineEdit()
        self._widgets["feishu.base_url"].setPlaceholderText(
            "https://open.feishu.cn/open-apis"
        )
        feishu_form.addRow("API 地址:", self._widgets["feishu.base_url"])

        layout.addWidget(feishu_group)

        # ── 串口通信设置（报警灯 / PLC）──
        serial_group = QGroupBox("串口通信（报警灯 / PLC）")
        serial_form = QFormLayout(serial_group)
        serial_form.setSpacing(8)

        # 启用开关
        self._widgets["serial.enabled"] = QCheckBox("启用串口通信")
        serial_form.addRow("", self._widgets["serial.enabled"])

        # 端口号（可下拉选已检测到的端口，也支持手动输入）
        self._widgets["serial.port"] = QComboBox()
        self._widgets["serial.port"].setEditable(True)
        self._refresh_serial_ports()
        port_row = QHBoxLayout()
        port_row.setSpacing(6)
        port_row.addWidget(self._widgets["serial.port"], 1)
        refresh_btn = QPushButton("刷新")
        refresh_btn.setFixedWidth(60)
        refresh_btn.clicked.connect(self._refresh_serial_ports)
        port_row.addWidget(refresh_btn)
        serial_form.addRow("端口号:", port_row)

        # 波特率（常用值下拉，也可手动输入）
        self._widgets["serial.baudrate"] = QComboBox()
        self._widgets["serial.baudrate"].setEditable(True)
        self._widgets["serial.baudrate"].addItems(
            ["9600", "19200", "38400", "57600", "115200"]
        )
        serial_form.addRow("波特率:", self._widgets["serial.baudrate"])

        # NG 信号
        self._widgets["serial.ng_signal"] = QLineEdit()
        self._widgets["serial.ng_signal"].setPlaceholderText(
            "HEX 字节帧，如: A0 00 01 CC （也兼容 ASCII: NG\\r\\n）"
        )
        serial_form.addRow("NG 信号:", self._widgets["serial.ng_signal"])

        # 复位信号
        self._widgets["serial.reset_signal"] = QLineEdit()
        self._widgets["serial.reset_signal"].setPlaceholderText(
            "HEX 字节帧，如: A0 00 00 CC （也兼容 ASCII: RESET\\r\\n）"
        )
        serial_form.addRow("复位信号:", self._widgets["serial.reset_signal"])

        serial_hint = QLabel(
            "通信参数: 8 数据位、无校验、1 停止位、无流控（8N1 none）\n"
            "信号格式（自动识别）:\n"
            "  • HEX 字节帧:  A0 00 01 CC  或  0xA0,0x00,0x01,0xCC  或  [0xA0,0x00,0x01,0xCC]\n"
            "  • ASCII 文本:  NG\\r\\n  （裸字符串，支持 \\r \\n \\t 转义）\n"
            "默认帧结构: A0(帧头) 00(保留) 01/00(NG=01 复位=00) CC(帧尾)"
        )
        serial_hint.setStyleSheet(
            "color: #8888a0; font-size: 11px; background: transparent; border: none;"
        )
        serial_hint.setWordWrap(True)
        serial_form.addRow(serial_hint)

        layout.addWidget(serial_group)

        # ── MES HTTP 上传设置 ──
        http_group = QGroupBox("MES 上传（HTTP 接口）")
        http_form = QFormLayout(http_group)
        http_form.setSpacing(8)

        # 启用开关
        self._widgets["http.enabled"] = QCheckBox("启用 MES 上传（仅 NG 结果会上传）")
        http_form.addRow("", self._widgets["http.enabled"])

        # 接口地址
        self._widgets["http.url"] = QLineEdit()
        self._widgets["http.url"].setPlaceholderText(
            "http://10.31.20.29/MesAPI/Api/WMSToMESByProcedure"
        )
        http_form.addRow("接口地址:", self._widgets["http.url"])

        # 超时时间
        self._widgets["http.timeout"] = QDoubleSpinBox()
        self._widgets["http.timeout"].setRange(1.0, 120.0)
        self._widgets["http.timeout"].setDecimals(1)
        self._widgets["http.timeout"].setSingleStep(1.0)
        self._widgets["http.timeout"].setSuffix(" 秒")
        self._widgets["http.timeout"].setValue(10.0)
        http_form.addRow("请求超时:", self._widgets["http.timeout"])

        # HEAD 字段表格（可增删）
        head_label = QLabel("HEAD 字段（可增加、修改、删除）:")
        head_label.setStyleSheet(
            "color: #e0e0e0; font-size: 12px; background: transparent; border: none;"
        )
        http_form.addRow(head_label)

        self._http_head_table = QTableWidget(0, 2)
        self._http_head_table.setHorizontalHeaderLabels(["字段名 (Key)", "字段值 (Value)"])
        self._http_head_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._http_head_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._http_head_table.verticalHeader().setVisible(False)
        self._http_head_table.setMinimumHeight(180)
        self._http_head_table.setStyleSheet(
            "QTableWidget { background-color: #0d1b2a; border: 1px solid #0f3460;"
            " border-radius: 4px; color: #e0e0e0; gridline-color: #0f3460; }"
            "QTableWidget::item { padding: 4px 6px; }"
            "QTableWidget::item:selected { background-color: #0f3460; }"
            "QHeaderView::section { background-color: #16213e; color: #e0e0e0;"
            " border: none; border-bottom: 1px solid #0f3460; padding: 4px; }"
        )
        http_form.addRow(self._http_head_table)

        # HEAD 操作按钮
        http_btn_row = QHBoxLayout()
        http_btn_row.setSpacing(8)
        add_head_btn = QPushButton("添加字段")
        add_head_btn.clicked.connect(self._add_http_head_field)
        http_btn_row.addWidget(add_head_btn)

        remove_head_btn = QPushButton("删除选中")
        remove_head_btn.clicked.connect(self._remove_http_head_field)
        http_btn_row.addWidget(remove_head_btn)

        reset_head_btn = QPushButton("恢复默认")
        reset_head_btn.clicked.connect(self._reset_http_head_fields)
        http_btn_row.addWidget(reset_head_btn)

        http_btn_row.addStretch()
        http_form.addRow(http_btn_row)

        http_hint = QLabel(
            "说明：只有检测结果为 NG 时才会上传到 MES。\n"
            "BODY 字段由 TCP 数据自动填充：BlockCode=晶编，CryptoschisisLength=隐裂长度，\n"
            "FilePath=图片路径，Generatedate=当前上传时间。\n"
            "上传成功/失败会在主界面实时显示。"
        )
        http_hint.setStyleSheet(
            "color: #8888a0; font-size: 11px; background: transparent; border: none;"
        )
        http_hint.setWordWrap(True)
        http_form.addRow(http_hint)

        layout.addWidget(http_group)

        # ── 图像存储设置 ──
        img_group = QGroupBox("图像存储设置")
        img_form = QFormLayout(img_group)
        img_form.setSpacing(8)
        self._widgets["image_store.enabled"] = QCheckBox("启用图像自动保存")
        img_form.addRow("", self._widgets["image_store.enabled"])
        self._widgets["image_store.base_dir"] = QLineEdit("D:/SiRod/images")
        img_form.addRow("存储目录:", self._widgets["image_store.base_dir"])

        img_desc = QLabel(
            "图像按 日期/缺陷类型 分文件夹存储。\n"
            "例如: D:/SiRod/images/2026-04-09/隐裂/0001_153208.png"
        )
        img_desc.setStyleSheet(
            "color: #8888a0; font-size: 11px; background: transparent; border: none;"
        )
        img_desc.setWordWrap(True)
        img_form.addRow(img_desc)

        layout.addWidget(img_group)

        # ── 产线设置 ──
        line_group = QGroupBox("产线设置")
        line_form = QFormLayout(line_group)
        line_form.setSpacing(8)
        self._widgets["line_id"] = QLineEdit("PV-B02")
        line_form.addRow("产线标识:", self._widgets["line_id"])
        layout.addWidget(line_group)

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

    # ─────────── HTTP HEAD 字段管理 ───────────

    _DEFAULT_HTTP_HEAD = {
        "DEST_SYSTEM": "YC01MES",
        "INTF_ID": "QPMES201",
        "SRC_SYSTEM": "YinLieJianCe",
        "SRC_MSGID": "",
        "BACKUP1": "QPMES201_CryptoschisisDataEM",
        "BACKUP2": "GRZ",
    }

    def _add_http_head_field(self):
        """在 HEAD 表末尾新增一行空字段"""
        row = self._http_head_table.rowCount()
        self._http_head_table.insertRow(row)
        self._http_head_table.setItem(row, 0, QTableWidgetItem(""))
        self._http_head_table.setItem(row, 1, QTableWidgetItem(""))
        # 自动编辑新行的 key 单元格
        self._http_head_table.editItem(self._http_head_table.item(row, 0))

    def _remove_http_head_field(self):
        """删除选中行"""
        rows = sorted({idx.row() for idx in self._http_head_table.selectedIndexes()},
                      reverse=True)
        if not rows:
            QMessageBox.information(self, "提示", "请先选择要删除的 HEAD 字段行")
            return
        for r in rows:
            self._http_head_table.removeRow(r)

    def _reset_http_head_fields(self):
        """恢复 HEAD 字段为默认值"""
        reply = QMessageBox.question(
            self, "确认", "确定要将 HEAD 字段恢复为默认值吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._set_http_head_table(self._DEFAULT_HTTP_HEAD)

    def _set_http_head_table(self, head_dict: dict):
        """把字典填入 HEAD 表格"""
        self._http_head_table.setRowCount(0)
        if not isinstance(head_dict, dict):
            return
        for key, value in head_dict.items():
            row = self._http_head_table.rowCount()
            self._http_head_table.insertRow(row)
            self._http_head_table.setItem(row, 0, QTableWidgetItem(str(key)))
            self._http_head_table.setItem(row, 1, QTableWidgetItem(str(value)))

    def _get_http_head_from_table(self) -> dict:
        """从表格读取 HEAD 字典（忽略 key 为空的行；保留重复 key 的最后一次出现）"""
        head = {}
        for row in range(self._http_head_table.rowCount()):
            key_item = self._http_head_table.item(row, 0)
            val_item = self._http_head_table.item(row, 1)
            key = (key_item.text().strip() if key_item else "")
            value = (val_item.text() if val_item else "")
            if not key:
                continue  # 忽略空 key
            head[key] = value
        return head

    # ─────────── 串口辅助 ───────────

    def _refresh_serial_ports(self):
        """扫描系统可用串口并填入下拉框（保留当前选中值）"""
        try:
            import serial.tools.list_ports
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception as e:
            logger.warning(f"扫描串口失败: {e}")
            ports = []

        combo = self._widgets.get("serial.port")
        if combo is None:
            return

        current = combo.currentText() if combo.count() > 0 else ""
        combo.clear()
        if ports:
            combo.addItems(ports)
        else:
            combo.addItem("COM3")  # 空列表时给个占位
        # 保留用户原本填的端口号（即使当前未检测到）
        if current and combo.findText(current) < 0:
            combo.insertItem(0, current)
        if current:
            combo.setCurrentText(current)

    # ─────────── 缺陷类型管理 ───────────

    def _add_defect_type(self):
        """弹出对话框添加新的缺陷类型"""
        text, ok = QInputDialog.getText(
            self, "添加缺陷类型", "请输入缺陷类型名称："
        )
        if ok and text.strip():
            name = text.strip()
            # 检查重复
            existing = self._get_defect_types_from_list()
            if name in existing:
                QMessageBox.warning(self, "提示", f"缺陷类型 \"{name}\" 已存在")
                return
            self._defect_type_list.addItem(name)

    def _remove_defect_type(self):
        """删除选中的缺陷类型"""
        current = self._defect_type_list.currentRow()
        if current >= 0:
            item = self._defect_type_list.item(current)
            name = item.text()
            reply = QMessageBox.question(
                self, "确认删除",
                f"确定要删除缺陷类型 \"{name}\" 吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._defect_type_list.takeItem(current)
        else:
            QMessageBox.information(self, "提示", "请先选择要删除的缺陷类型")

    def _get_defect_types_from_list(self) -> list:
        """从列表控件获取所有缺陷类型"""
        types = []
        for i in range(self._defect_type_list.count()):
            types.append(self._defect_type_list.item(i).text())
        return types

    def get_defect_types(self) -> list:
        """获取当前配置的缺陷类型列表（供外部调用）"""
        if self._config:
            return self._config.get("defect_types", ["隐裂", "崩边"])
        return self._get_defect_types_from_list()

    # ─────────── 配置加载/保存 ───────────

    def _load_from_config(self):
        if not self._config:
            return
        for key, widget in self._widgets.items():
            val = self._config.get(key)
            if val is None:
                continue
            if isinstance(widget, QLineEdit):
                widget.setText(str(val))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(val))
            elif isinstance(widget, QDoubleSpinBox):
                try:
                    widget.setValue(float(val))
                except (TypeError, ValueError):
                    pass
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(val))
            elif isinstance(widget, QComboBox):
                # 可编辑下拉：若值不在选项里则临时插到首位
                text = str(val)
                if widget.findText(text) < 0:
                    widget.insertItem(0, text)
                widget.setCurrentText(text)

        # 加载 HTTP HEAD 字段表
        http_head = self._config.get("http.head", {})
        self._set_http_head_table(http_head if isinstance(http_head, dict) else {})

        # 加载班次清零时间
        reset_times = self._config.get("shift.reset_times", ["08:00", "20:00"])
        if isinstance(reset_times, list) and len(reset_times) >= 2:
            self._set_time_edit(self._shift_time_1, reset_times[0])
            self._set_time_edit(self._shift_time_2, reset_times[1])
        elif isinstance(reset_times, list) and len(reset_times) == 1:
            self._set_time_edit(self._shift_time_1, reset_times[0])

        # 加载缺陷类型列表
        defect_types = self._config.get("defect_types", ["隐裂", "崩边"])
        self._defect_type_list.clear()
        if isinstance(defect_types, list):
            for dt in defect_types:
                self._defect_type_list.addItem(str(dt))

    def _save_settings(self):
        if not self._config:
            QMessageBox.warning(self, "提示", "配置对象未初始化")
            return
        for key, widget in self._widgets.items():
            if isinstance(widget, QLineEdit):
                self._config.set(key, widget.text())
            elif isinstance(widget, QSpinBox):
                self._config.set(key, widget.value())
            elif isinstance(widget, QDoubleSpinBox):
                self._config.set(key, widget.value())
            elif isinstance(widget, QCheckBox):
                self._config.set(key, widget.isChecked())
            elif isinstance(widget, QComboBox):
                text = widget.currentText().strip()
                # baudrate 要存为 int
                if key == "serial.baudrate":
                    try:
                        self._config.set(key, int(text))
                    except ValueError:
                        self._config.set(key, 9600)
                else:
                    self._config.set(key, text)

        # 保存班次清零时间
        time_1 = self._shift_time_1.time().toString("HH:mm")
        time_2 = self._shift_time_2.time().toString("HH:mm")
        reset_times = sorted([time_1, time_2])
        self._config.set("shift.reset_times", reset_times)

        # 保存缺陷类型列表
        defect_types = self._get_defect_types_from_list()
        self._config.set("defect_types", defect_types)

        # 保存 HTTP HEAD 字段表
        http_head = self._get_http_head_from_table()
        self._config.set("http.head", http_head)

        self._config.save()

        # 通知缺陷图库更新类型列表
        self.defect_types_changed.emit(defect_types)
        # 通知 main.py 重新打开串口
        self.serial_settings_changed.emit()
        # 通知 main.py HTTP 配置已变更
        self.http_settings_changed.emit()

        QMessageBox.information(self, "成功", "设置已保存！\n部分设置需要重启软件后生效。")

    def get_shift_reset_times(self) -> list:
        """获取当前设置界面上的班次清零时间"""
        time_1 = self._shift_time_1.time().toString("HH:mm")
        time_2 = self._shift_time_2.time().toString("HH:mm")
        return sorted([time_1, time_2])

    @staticmethod
    def _set_time_edit(time_edit: QTimeEdit, time_str: str):
        """将 "HH:mm" 格式字符串设置到 QTimeEdit"""
        try:
            parts = time_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            time_edit.setTime(QTime(hour, minute))
        except (ValueError, IndexError):
            pass