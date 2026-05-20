"""
相机参数页（顶部「相机」tab）
================================
功能：
  - 展示设备只读信息（型号 / 序列号 / IP / MAC / 厂商）
  - 显示硬件实时值 vs 程序配置值的对比
  - 可改帧参数（Width / Height / AcquisitionMode / AcquisitionFrameCount）
  - 可改触发参数（TriggerMode / TriggerSource / ExposureTime）
  - 可改软件层参数（multiframe_first_wait_s / grab_timeout_ms）

页面打开自动调一次 read_camera_params；用户可手动「重新读取」。
保存前弹确认对话框（表格显示新旧 diff），确认后立刻：
  1) 写硬件（cam.stop + cam.configure + cam.start）
  2) 持久化 config.json
  3) UI 刷新

UI 不直接访问 engine：通过两个回调注入（reader / applier），保持解耦。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PyQt6.QtCore import (
    Qt, pyqtSignal, QLocale, QObject, QRunnable, QThreadPool,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QGroupBox, QFormLayout, QPushButton,
    QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox,
    QMessageBox, QScrollArea,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QDialog, QDialogButtonBox,
)


# ============================================================
# 异步执行 wrapper（避免 UI 主线程被相机锁阻塞 20s）
# ============================================================
# 原因：trigger_once 持 _camera_lock 最长 = grab_timeout_ms (20s)
# UI 直接调 reader/applier 会抢锁 → 主线程卡 → 「未响应」
# 解：丢到 QThreadPool 后台跑，完事用 signal 回主线程刷 UI

class _CamOpSignals(QObject):
    finished = pyqtSignal(object)   # dict (reader 返回) 或 None (applier 完成)
    error    = pyqtSignal(str)


class _CamOpRunnable(QRunnable):
    """通用相机操作 worker"""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = _CamOpSignals()

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.signals.finished.emit(result)
        except Exception as e:
            self.signals.error.emit(f"{type(e).__name__}: {e}")

logger = logging.getLogger("SiRod.CameraPage")

_C_LOCALE = QLocale(QLocale.Language.C)

_ACQUISITION_MODES = ["SingleFrame", "MultiFrame", "Continuous"]
_TRIGGER_MODES = ["On", "Off"]
_TRIGGER_SOURCES = ["Software", "Line0", "Line1", "FrequencyConverter"]


def _style_spin(sp):
    sp.setLocale(_C_LOCALE)
    sp.setMinimumWidth(120)
    sp.setStyleSheet(
        "QAbstractSpinBox { padding-right: 18px; padding-left: 4px; }"
    )


class CameraPage(QWidget):
    """相机参数页面。

    外部通过两个 setter 注入依赖（避免硬耦合 InspectEngine）::

        page.set_reader(engine.read_camera_params)   # () -> dict
        page.set_applier(engine.apply_camera_params) # (dict) -> None
        page.set_config(app_config)                   # 用于持久化
    """

    # 保存成功（已经写硬件 + 持久化到 config.json）后发出。
    # 调用方可以更新其他依赖（比如刷新底部状态栏）
    params_saved = pyqtSignal(dict)

    def __init__(self, config=None,
                 reader: Optional[Callable[[], dict]] = None,
                 applier: Optional[Callable[[dict], None]] = None):
        super().__init__()
        self._config = config
        self._reader = reader
        self._applier = applier
        # 最后一次读到的硬件值（用于"取消修改"恢复）
        self._last_hw_params: dict = {}
        # 可编辑字段的控件（key 跟 read_camera_params 的字段对齐）
        self._editors: dict = {}
        # 硬件实时值的展示 label
        self._hw_labels: dict = {}
        # 设备只读信息 label
        self._info_labels: dict = {}
        # showEvent 防抖时间戳：避免 QStackedWidget 切换 / 窗口最小化恢复
        # 等场景导致 showEvent 被频繁触发 → read_camera_params 反复抢相机锁
        self._last_read_ts: float = 0.0
        self._init_ui()
        # ★ 关键 bug 修复：__init__ 时必须从 config 加载值到控件，
        # 否则 spinbox 保持默认值（被 setRange clamp 到 min），
        # 用户点保存会把这些错误默认值写回 config。
        # 之前只在 set_config() 里调，但 main_camera 用 __init__ 传 config
        # 不会触发 set_config，导致 grab_timeout_ms 一直显示 100（min）。
        if config is not None:
            self._load_from_config()

    def set_reader(self, reader: Callable[[], dict]) -> None:
        self._reader = reader

    def set_applier(self, applier: Callable[[dict], None]) -> None:
        self._applier = applier

    def set_config(self, config) -> None:
        self._config = config
        self._load_from_config()

    def showEvent(self, event):
        """页面被切到前台时自动读一次硬件值（用户需求）。
        silent=True：engine 没起来时不弹错误框，等手动点按钮"""
        super().showEvent(event)
        # 防抖：避免 QStackedWidget 切换 / 窗口状态变化等触发频繁 showEvent，
        # 跟 worker 的 trigger_once 抢相机锁（每次 read 也加锁，read 期间
        # 可能影响 SDK 状态）。2 秒内只允许 read 一次。
        now = time.time()
        if now - self._last_read_ts < 2.0:
            return
        self._last_read_ts = now
        # 用户切到本 tab 时自动读最新硬件值
        self.refresh_from_hardware(silent=True)

    # ─────────── 初始化 UI ───────────

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(14)

        # ── 设备信息（只读）──
        info_group = QGroupBox("设备信息（只读）")
        info_form = QFormLayout(info_group)
        info_form.setSpacing(8)
        for key, label in [
            ("model", "型号"),
            ("serial", "序列号"),
            ("vendor", "厂商"),
            ("ip_addr", "IP 地址"),
            ("mac_addr", "MAC 地址"),
        ]:
            lbl = QLabel("（未读取）")
            lbl.setStyleSheet(
                "background: transparent; border: none; color: #b0b0c0;"
            )
            self._info_labels[key] = lbl
            info_form.addRow(label + ":", lbl)
        layout.addWidget(info_group)

        # ── 帧参数 ──
        frame_group = QGroupBox(
            "帧参数（影响图像尺寸；保存会重启采集流）"
        )
        frame_layout = QFormLayout(frame_group)
        frame_layout.setSpacing(10)

        self._editors["width"] = QSpinBox()
        self._editors["width"].setRange(1, 16384)
        _style_spin(self._editors["width"])
        self._hw_labels["width"] = QLabel("—")
        frame_layout.addRow("Width:", self._editor_row("width"))

        self._editors["height"] = QSpinBox()
        self._editors["height"].setRange(1, 65536)
        _style_spin(self._editors["height"])
        self._hw_labels["height"] = QLabel("—")
        frame_layout.addRow("Height:", self._editor_row("height"))

        self._editors["acquisition_mode"] = QComboBox()
        self._editors["acquisition_mode"].addItems(_ACQUISITION_MODES)
        self._hw_labels["acquisition_mode"] = QLabel("—")
        frame_layout.addRow("AcquisitionMode:",
                            self._editor_row("acquisition_mode"))

        self._editors["acquisition_frame_count"] = QSpinBox()
        self._editors["acquisition_frame_count"].setRange(1, 10000)
        _style_spin(self._editors["acquisition_frame_count"])
        self._hw_labels["acquisition_frame_count"] = QLabel("—")
        frame_layout.addRow("AcquisitionFrameCount:",
                            self._editor_row("acquisition_frame_count"))

        layout.addWidget(frame_group)

        # ── 触发设置 ──
        trig_group = QGroupBox("触发设置")
        trig_layout = QFormLayout(trig_group)
        trig_layout.setSpacing(10)

        self._editors["trigger_mode"] = QComboBox()
        self._editors["trigger_mode"].addItems(_TRIGGER_MODES)
        self._hw_labels["trigger_mode"] = QLabel("—")
        trig_layout.addRow("TriggerMode:", self._editor_row("trigger_mode"))

        self._editors["trigger_source"] = QComboBox()
        self._editors["trigger_source"].addItems(_TRIGGER_SOURCES)
        self._hw_labels["trigger_source"] = QLabel("—")
        trig_layout.addRow("TriggerSource:",
                           self._editor_row("trigger_source"))

        self._editors["exposure_us"] = QDoubleSpinBox()
        self._editors["exposure_us"].setRange(0.0, 1_000_000.0)
        self._editors["exposure_us"].setSuffix(" us")
        self._editors["exposure_us"].setSpecialValueText("沿用当前（不改）")
        self._editors["exposure_us"].setDecimals(1)
        _style_spin(self._editors["exposure_us"])
        self._hw_labels["exposure_us"] = QLabel("—")
        trig_layout.addRow("ExposureTime:",
                           self._editor_row("exposure_us"))

        layout.addWidget(trig_group)

        # ── 软件层参数 ──
        sw_group = QGroupBox(
            "软件层参数（程序内部控制，不在相机寄存器）"
        )
        sw_layout = QFormLayout(sw_group)
        sw_layout.setSpacing(10)

        self._editors["multiframe_first_wait_s"] = QDoubleSpinBox()
        self._editors["multiframe_first_wait_s"].setRange(0.0, 60.0)
        self._editors["multiframe_first_wait_s"].setSuffix(" s")
        self._editors["multiframe_first_wait_s"].setDecimals(1)
        _style_spin(self._editors["multiframe_first_wait_s"])
        sw_layout.addRow(
            "多帧首帧等待 (对齐 Halcon wait_seconds(5)):",
            self._editors["multiframe_first_wait_s"],
        )

        self._editors["grab_timeout_ms"] = QSpinBox()
        self._editors["grab_timeout_ms"].setRange(100, 600_000)
        self._editors["grab_timeout_ms"].setSuffix(" ms")
        _style_spin(self._editors["grab_timeout_ms"])
        sw_layout.addRow(
            "单帧 ImageComplete Timeout:",
            self._editors["grab_timeout_ms"],
        )

        layout.addWidget(sw_group)

        # ── 操作按钮 ──
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(10)

        self._btn_read = QPushButton("重新读取硬件值")
        self._btn_read.clicked.connect(self.refresh_from_hardware)
        btn_bar.addWidget(self._btn_read)

        btn_bar.addStretch()

        self._btn_cancel = QPushButton("取消修改（恢复上次硬件值）")
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_bar.addWidget(self._btn_cancel)

        self._btn_save = QPushButton("保存并重启采集 ★")
        self._btn_save.setStyleSheet(
            "QPushButton { background-color: #00d4ff; color: #000;"
            "  font-weight: bold; padding: 6px 14px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #00b8e0; }"
        )
        self._btn_save.clicked.connect(self._on_save)
        btn_bar.addWidget(self._btn_save)

        layout.addLayout(btn_bar)

        layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _editor_row(self, key: str) -> QWidget:
        """编辑控件 + 右侧"硬件当前值"label 同一行布局"""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        h.addWidget(self._editors[key], 1)

        hw_wrap = QHBoxLayout()
        hw_wrap.setContentsMargins(0, 0, 0, 0)
        hw_wrap.setSpacing(4)
        hw_tag = QLabel("硬件:")
        hw_tag.setStyleSheet(
            "color: #8888a0; background: transparent; border: none;"
            " font-size: 11px;"
        )
        hw_wrap.addWidget(hw_tag)

        self._hw_labels[key].setStyleSheet(
            "background: transparent; border: none; color: #b0b0c0;"
            " min-width: 90px;"
        )
        hw_wrap.addWidget(self._hw_labels[key])
        h.addLayout(hw_wrap)
        return row

    # ─────────── 数据加载 ───────────

    def _load_from_config(self) -> None:
        """从 config 把"程序配置"载入到编辑控件（页面初次显示时调）"""
        if self._config is None:
            return
        cfg = self._config

        try:
            self._editors["width"].setValue(int(cfg.get("camera.width", 1024)))
            self._editors["height"].setValue(int(cfg.get("camera.height", 100)))

            mode = str(cfg.get("camera.acquisition_mode", "SingleFrame"))
            idx = (_ACQUISITION_MODES.index(mode)
                   if mode in _ACQUISITION_MODES else 0)
            self._editors["acquisition_mode"].setCurrentIndex(idx)

            fc = cfg.get("camera.acquisition_frame_count", 1)
            self._editors["acquisition_frame_count"].setValue(
                int(fc) if fc else 1
            )

            tmode = str(cfg.get("camera.trigger_mode", "On"))
            t_idx = (_TRIGGER_MODES.index(tmode)
                     if tmode in _TRIGGER_MODES else 0)
            self._editors["trigger_mode"].setCurrentIndex(t_idx)

            tsrc = str(cfg.get("camera.trigger_source", "Software"))
            if tsrc in _TRIGGER_SOURCES:
                self._editors["trigger_source"].setCurrentIndex(
                    _TRIGGER_SOURCES.index(tsrc)
                )

            exp = cfg.get("camera.exposure_us", None)
            self._editors["exposure_us"].setValue(
                float(exp) if exp else 0.0
            )

            self._editors["multiframe_first_wait_s"].setValue(
                float(cfg.get("camera.multiframe_first_wait_s", 0.0))
            )
            self._editors["grab_timeout_ms"].setValue(
                int(cfg.get("camera.grab_timeout_ms", 10000))
            )
        except Exception as e:
            logger.warning(f"从 config 加载相机参数失败: {e}", exc_info=True)

    def refresh_from_hardware(self, silent: bool = False) -> None:
        """从相机硬件读当前值，刷新"硬件"列；不动用户编辑的控件。

        ★ 异步执行 ★ — 调用立即返回。reader 在 QThreadPool 后台跑，
        避免持续阻塞 UI 主线程（reader 抢 _camera_lock 最长可等 20s）。
        读取完成/失败后 signal 回主线程更新 UI。

        silent=True 时失败只 log 不弹框（用于 showEvent 自动读）。
        """
        if self._reader is None:
            if not silent:
                QMessageBox.warning(
                    self, "未连接", "InspectEngine 未启动，无法读相机参数。"
                )
            return

        # 防并发：上一次读还没回来就跳过
        if getattr(self, "_read_in_progress", False):
            logger.debug("相机参数读取进行中，跳过重复请求")
            return
        self._read_in_progress = True

        runnable = _CamOpRunnable(self._reader)
        runnable.signals.finished.connect(
            lambda params: self._on_read_done(params, silent)
        )
        runnable.signals.error.connect(
            lambda err: self._on_read_error(err, silent)
        )
        QThreadPool.globalInstance().start(runnable)
        logger.debug("相机参数读取已提交后台线程")

    def _on_read_done(self, params: dict, silent: bool) -> None:
        """reader 后台跑完后回到主线程刷 UI"""
        self._read_in_progress = False
        if not isinstance(params, dict):
            logger.warning(f"reader 返回非 dict: {type(params)}")
            return

        self._last_hw_params = dict(params)
        self._last_read_ts = time.time()  # 同步更新防抖时间戳

        # 填只读设备信息
        for key in ("model", "serial", "vendor", "ip_addr", "mac_addr"):
            val = params.get(key, "")
            self._info_labels[key].setText(str(val) if val else "（未知）")

        # 填硬件实时值 + 跟编辑控件对比，不一致时黄色高亮
        for key, label in self._hw_labels.items():
            hw_val = params.get(key)
            label.setText("（读取失败）" if hw_val is None else str(hw_val))
            if hw_val is None:
                label.setStyleSheet(
                    "background: transparent; border: none; color: #e74c3c;"
                    " min-width: 90px;"
                )
                continue
            ui_val = self._get_editor_value(key)
            mismatch = (
                hw_val != ui_val
                and str(hw_val) != str(ui_val)
                # 数字类型容忍微小浮点差
                and not (isinstance(hw_val, (int, float))
                         and isinstance(ui_val, (int, float))
                         and abs(float(hw_val) - float(ui_val)) < 1e-3)
            )
            label.setStyleSheet(
                "background: transparent; border: none;"
                + (" color: #f39c12; font-weight: bold;"
                   if mismatch else " color: #27ae60;")
                + " min-width: 90px;"
            )
        # log 列 key=value（不只 keys），让看 log 能直接对比 BV Viewer 设置
        editable_keys = ("width", "height", "acquisition_mode",
                         "acquisition_frame_count", "trigger_mode",
                         "trigger_source", "exposure_us",
                         "multiframe_first_wait_s", "grab_timeout_ms")
        summary = ", ".join(
            f"{k}={params.get(k)}" for k in editable_keys
        )
        logger.info(f"已从硬件读取相机参数: {summary}")

    def _on_read_error(self, err: str, silent: bool) -> None:
        """reader 后台跑失败"""
        self._read_in_progress = False
        logger.warning(f"读相机参数失败: {err}")
        if not silent:
            QMessageBox.critical(self, "读取失败", f"读相机参数失败：\n{err}")

    # ─────────── 取值 / 保存 ───────────

    def _get_editor_value(self, key: str):
        """读编辑控件当前值，类型对齐 read_camera_params 返回 dict"""
        w = self._editors.get(key)
        if w is None:
            return None
        if isinstance(w, QSpinBox):
            return int(w.value())
        if isinstance(w, QDoubleSpinBox):
            # exposure_us 用 specialValueText 表示"不改"，对应返回 None
            if (key == "exposure_us"
                    and abs(w.value() - w.minimum()) < 1e-6):
                return None
            return float(w.value())
        if isinstance(w, QComboBox):
            return w.currentText()
        if isinstance(w, QLineEdit):
            return w.text()
        return None

    def _collect_ui_params(self) -> dict:
        """读所有编辑控件，返回 dict（同 apply_camera_params 参数格式）"""
        out = {}
        for key in self._editors:
            out[key] = self._get_editor_value(key)
        return out

    def _on_cancel(self) -> None:
        """恢复成"上次读到的硬件值"或 config 值"""
        if self._last_hw_params:
            # 用硬件值回填
            for key, val in self._last_hw_params.items():
                if key in self._editors and val is not None:
                    self._set_editor_value(key, val)
        else:
            self._load_from_config()

    def _set_editor_value(self, key: str, val) -> None:
        w = self._editors.get(key)
        if w is None:
            return
        try:
            if isinstance(w, QSpinBox):
                w.setValue(int(val))
            elif isinstance(w, QDoubleSpinBox):
                w.setValue(float(val))
            elif isinstance(w, QComboBox):
                text = str(val)
                idx = w.findText(text)
                if idx >= 0:
                    w.setCurrentIndex(idx)
            elif isinstance(w, QLineEdit):
                w.setText(str(val))
        except Exception as e:
            logger.warning(f"设置控件 {key}={val} 失败: {e}")

    def _on_save(self) -> None:
        """点击「保存并重启采集」

        ★ 异步执行 ★ — applier 在 QThreadPool 后台跑（stop + configure + start
        全套 BV SDK 调用最长 5s+），避免 UI 主线程卡死。
        按钮变灰 + 文字变"应用中..."，完成后回调刷 UI。
        """
        if self._applier is None or self._reader is None:
            QMessageBox.warning(
                self, "未连接", "InspectEngine 未启动，无法保存。"
            )
            return

        # 防并发：上一次保存还没回来就跳过
        if getattr(self, "_save_in_progress", False):
            QMessageBox.information(
                self, "请稍候", "上一次保存正在进行中..."
            )
            return

        ui_params = self._collect_ui_params()

        # 拿"当前硬件值"做 diff（如果之前没读过，沿用 config 或编辑控件值）
        current = self._last_hw_params
        if not current:
            # 没读过硬件值 → 不做精细 diff，直接全部当变化
            # （避免在这里同步调 reader 阻塞 UI；用户应该先点"重新读取"）
            QMessageBox.information(
                self, "请先读取",
                "尚未读取过当前硬件值。先点「重新读取硬件值」让对比生效。"
            )
            return

        # 找出真正变化的字段
        diffs = []
        for key, new_val in ui_params.items():
            old_val = current.get(key)
            # exposure_us 特殊：None vs 数字都算变化
            if key == "exposure_us":
                if new_val is None and old_val is None:
                    continue
            if old_val != new_val:
                diffs.append((key, old_val, new_val))

        if not diffs:
            QMessageBox.information(
                self, "无修改",
                "所有参数跟当前硬件 / 配置一致，无需保存。"
            )
            return

        # 弹确认对话框（带 diff 表格）
        if not _confirm_save(self, diffs):
            return

        # ─── 异步执行 applier ───
        self._save_in_progress = True
        self._pending_save_params = dict(ui_params)
        # 按钮变灰 + 改文字
        original_text = self._btn_save.text()
        self._btn_save.setEnabled(False)
        self._btn_save.setText("应用中... (后台执行，UI 不卡)")
        self._btn_cancel.setEnabled(False)
        self._original_save_text = original_text

        runnable = _CamOpRunnable(self._applier, ui_params)
        runnable.signals.finished.connect(lambda _r: self._on_save_done())
        runnable.signals.error.connect(self._on_save_error)
        QThreadPool.globalInstance().start(runnable)
        logger.info(f"相机参数应用已提交后台线程: {ui_params}")

    def _on_save_done(self) -> None:
        """applier 后台执行成功，回主线程持久化 + 刷 UI"""
        self._save_in_progress = False
        ui_params = getattr(self, "_pending_save_params", {}) or {}

        # 恢复按钮
        self._btn_save.setEnabled(True)
        self._btn_save.setText(
            getattr(self, "_original_save_text", "保存并重启采集 ★")
        )
        self._btn_cancel.setEnabled(True)

        # 持久化到 config.json
        if self._config is not None and ui_params:
            try:
                for key, val in ui_params.items():
                    self._config.set(f"camera.{key}", val)
                self._config.save()
                logger.info("相机参数已持久化到 config.json")
            except Exception as e:
                logger.error(f"持久化 config.json 失败: {e}", exc_info=True)
                QMessageBox.warning(
                    self, "持久化失败",
                    f"相机参数已应用但写 config.json 失败：\n{e}\n"
                    "下次启动还是按原值。"
                )

        # 应用后立刻重新读硬件刷新 UI（也是异步的，不会阻塞）
        self.refresh_from_hardware(silent=True)
        self.params_saved.emit(dict(ui_params))
        QMessageBox.information(
            self, "已保存",
            "相机参数已生效，且写入 config.json。\n"
            "下次启动按当前值。"
        )

    def _on_save_error(self, err: str) -> None:
        """applier 后台执行失败"""
        self._save_in_progress = False
        # 恢复按钮
        self._btn_save.setEnabled(True)
        self._btn_save.setText(
            getattr(self, "_original_save_text", "保存并重启采集 ★")
        )
        self._btn_cancel.setEnabled(True)

        logger.error(f"应用相机参数失败: {err}")
        QMessageBox.critical(
            self, "应用失败",
            f"热更新相机参数失败：\n{err}\n\n"
            "config.json 未持久化，下次启动按原值。"
        )


# ============================================================
# 确认对话框
# ============================================================

def _confirm_save(parent: QWidget, diffs: list) -> bool:
    """弹"确认修改"对话框，diffs = [(key, old, new), ...]，返回 True=确认"""
    dlg = QDialog(parent)
    dlg.setWindowTitle("确认修改相机参数")
    dlg.setMinimumWidth(500)
    layout = QVBoxLayout(dlg)

    msg = QLabel(
        f"以下 <b>{len(diffs)}</b> 项参数将被修改，并 <b>立即重启相机采集流</b>。\n"
        "正在跑的一次抓图会失败（status=1），下次 iter 用新值。\n"
        "保存后会同步写入 config.json，下次启动按当前值。"
    )
    msg.setWordWrap(True)
    msg.setStyleSheet("background: transparent; border: none;")
    layout.addWidget(msg)

    tbl = QTableWidget(len(diffs), 3)
    tbl.setHorizontalHeaderLabels(["参数", "原值（硬件 / 当前）", "新值"])
    tbl.verticalHeader().setVisible(False)
    tbl.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.Stretch
    )
    tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for i, (key, old, new) in enumerate(diffs):
        tbl.setItem(i, 0, QTableWidgetItem(str(key)))
        old_item = QTableWidgetItem("（未读取/不支持）" if old is None else str(old))
        new_item = QTableWidgetItem("沿用当前（不改）" if new is None else str(new))
        tbl.setItem(i, 1, old_item)
        tbl.setItem(i, 2, new_item)
    layout.addWidget(tbl)

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认修改并重启采集")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    return dlg.exec() == QDialog.DialogCode.Accepted
