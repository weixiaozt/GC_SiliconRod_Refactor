"""
SiRod Inspector - 相机驱动模式入口
====================================
本模块是 ``main.py`` 的姊妹入口：

  ``main.py``         Halcon 模式 — 由外部 Run.bat (Halcon) 跑算法，
                       通过 HALCON TCP 协议把 ``InspectData`` 推给 UI
  ``main_camera.py``  相机模式 — Python 直接驱相机 + 算法（``InspectEngine``），
                       检测产物同样以 ``InspectData`` 喂给现有 UI

两者**消费侧完全相同**：UI / DB / 飞书 / MES / 串口报警的代码不动，
只是数据源从 TCPServer 换成 InspectEngine。原 ``main.py`` 保留以便回滚。

启动::

    python -m sirod_inspector.main_camera
    # 或
    python sirod_inspector/main_camera.py
"""

import sys
import os
import datetime
import threading
import traceback

# ── 路径设置 ──
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)
_PARENT_DIR = os.path.dirname(_PROJECT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

# ── 日志系统（最先初始化）──
from core.logger import setup_logging, get_logger

_log_dir = os.path.join(_PROJECT_DIR, "logs")
setup_logging(log_dir=_log_dir, level="INFO", keep_days=30)
logger = get_logger("SiRod.MainCamera")

logger.info("=" * 60)
logger.info("SiRod Inspector (Camera Mode) 启动中...")
logger.info(f"项目目录: {_PROJECT_DIR}")
logger.info(f"Python: {sys.version}")
logger.info("=" * 60)

# ── 导入模块 ──
try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from PyQt6.QtCore import QTimer, pyqtSignal, QObject
    from PyQt6.QtGui import QIcon

    from data.config import AppConfig
    from data.database import Database
    from data.feishu import FeishuSync
    from data.shift_stats import ShiftStats
    from core.tcp_server import InspectData     # 只用数据类，不启动 TCPServer
    from core.inspect_engine import InspectEngine, InspectEngineConfig
    from core.serial_manager import SerialManager
    from core.http_client import MesHttpClient
    from ui.styles import DARK_STYLE
    from ui.main_window import MainWindow
    from ui.overview_page import OverviewPage
    from ui.history_page import HistoryPage
    from ui.gallery_page import GalleryPage
    from ui.stats_page import StatsPage
    from ui.settings_page import SettingsPage
    from algorithm import JudgeConfig
except ImportError as e:
    logger.critical(f"模块导入失败: {e}", exc_info=True)
    print(f"\n[FATAL] 模块导入失败: {e}")
    print("请确保已安装所有依赖: "
          "pip install PyQt6 numpy Pillow pymysql matplotlib openpyxl requests pyserial opencv-python")
    sys.exit(1)


class SiRodCameraApp(QObject):
    """应用主控制器 — 相机驱动模式

    与 main.py 的差异：
      ✗ 移除 TCPServer / Run.bat 子进程管理
      ✓ 新增 InspectEngine（相机 + Pipeline 编排）
      ✓ 棒号来源为 ``rod_id_provider`` 回调（默认 'NoRead'，待扫码枪接入）

    其余 UI / DB / 飞书 / MES / 串口 / 班次 逻辑与 main.py 完全一致。
    """

    # 跨线程信号
    _inspect_data_signal = pyqtSignal(object)       # 检测线程 → UI 线程
    _mes_status_signal = pyqtSignal(bool, str, str) # 后台 HTTP → UI 线程

    def __init__(self):
        super().__init__()
        logger.info("初始化应用控制器（相机模式）...")

        # 配置
        config_path = os.path.join(_PROJECT_DIR, "config.json")
        self.config = AppConfig(config_path)
        logger.info(f"配置已加载: {config_path}")

        # 数据库
        db_cfg = self.config.get("database", {})
        self.database = Database(db_cfg)
        logger.info(
            f"数据库配置: {db_cfg.get('host', '127.0.0.1')}:{db_cfg.get('port', 3306)}"
            f"/{db_cfg.get('database', 'b_xmartsql')}"
        )

        # 飞书
        feishu_cfg = self.config.get("feishu", {})
        self.feishu = FeishuSync(feishu_cfg)

        # 串口
        self.serial_manager = SerialManager(self.config)

        # MES
        self.http_client = MesHttpClient(self.config)

        # ── 检测引擎（取代原 TCPServer + Run.bat）──
        engine_cfg = InspectEngineConfig(
            camera_uid=0,
            width=int(self.config.get("camera.width", 1024)),
            height=int(self.config.get("camera.height", 15000)),
            exposure_us=self.config.get("camera.exposure_us", None),
            trigger_source=self.config.get("camera.trigger_source", "Software"),
            grab_timeout_ms=int(self.config.get("camera.grab_timeout_ms", 10000)),
            seg_model=os.path.join(_PARENT_DIR, "models", "Model_seg.m"),
            cls_model=os.path.join(_PARENT_DIR, "models", "Model_cls.m"),
            judge_config=JudgeConfig(
                max_area=float(self.config.get("judge.max_area", 10)),
                sum_area=float(self.config.get("judge.sum_area", 10)),
                max_count=int(self.config.get("judge.max_count", 10)),
                max_length=float(self.config.get("judge.max_length", 2)),
            ),
        )
        # 棒号注入 — 当前 mock，后续替换为扫码枪客户端
        self._latest_rod_id = "NoRead"
        self._rod_id_lock = threading.Lock()

        def _rod_id_provider() -> str:
            with self._rod_id_lock:
                return self._latest_rod_id

        self.engine = InspectEngine(
            engine_cfg,
            rod_id_provider=_rod_id_provider,
            on_inspect=self._on_inspect_data,   # 工作线程
            on_error=lambda e: logger.error(f"InspectEngine: {e}"),
        )
        logger.info(f"检测引擎配置: {engine_cfg}")

        # 班次统计
        reset_times = self.config.get("shift.reset_times", ["08:00", "20:00"])
        self.shift_stats = ShiftStats(
            project_dir=_PROJECT_DIR, reset_times=reset_times,
        )

        # UI
        self.window = MainWindow()
        self.overview_page = OverviewPage()
        self.history_page = HistoryPage(database=self.database)
        self.gallery_page = GalleryPage()
        self.stats_page = StatsPage(database=self.database)
        self.settings_page = SettingsPage(config=self.config)

        for name, page in [
            ("overview", self.overview_page),
            ("history",  self.history_page),
            ("gallery",  self.gallery_page),
            ("stats",    self.stats_page),
            ("settings", self.settings_page),
        ]:
            self.window.add_page(name, page)
        logger.info("UI 页面已注册")

        self.overview_page.set_shift_stats(self.shift_stats)
        alarm_enabled = self.config.get("alarm.enabled", True)
        if hasattr(self.overview_page, "set_alarm_enabled"):
            self.overview_page.set_alarm_enabled(bool(alarm_enabled))

        # 信号 → UI 线程
        self._inspect_data_signal.connect(self._handle_inspect_data)
        self._mes_status_signal.connect(self._on_mes_status_updated)

        # 复位按钮
        if hasattr(self.overview_page, "reset_requested"):
            self.overview_page.reset_requested.connect(self._on_reset_clicked)

        # 报警开关
        if hasattr(self.overview_page, "alarm_enabled_changed"):
            self.overview_page.alarm_enabled_changed.connect(
                self._on_alarm_enabled_changed
            )

        # 串口设置变更 → 重连
        if hasattr(self.settings_page, "serial_settings_changed"):
            self.settings_page.serial_settings_changed.connect(
                self._on_serial_settings_changed
            )

        # 状态刷新定时器
        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(2000)

        # 班次清零定时器
        self._shift_timer = QTimer()
        self._shift_timer.timeout.connect(self._check_shift_reset)
        self._shift_timer.start(30_000)

        logger.info("应用控制器初始化完成")

    # ─────────── 启动 ───────────
    def start(self):
        """启动所有服务并显示窗口"""
        # 数据库
        try:
            if self.database.connect():
                self.window.set_device_status("数据库", True)
            else:
                self.window.set_device_status("数据库", False)
        except Exception as e:
            logger.error(f"数据库连接异常: {e}", exc_info=True)
            self.window.set_device_status("数据库", False)

        # 飞书
        try:
            if self.feishu.is_enabled:
                self.feishu.start()
                self.window.set_device_status("飞书", True)
        except Exception as e:
            logger.error(f"飞书同步启动失败: {e}", exc_info=True)

        # 串口
        try:
            if self.serial_manager.open():
                self.window.set_device_status("报警灯", True)
            else:
                self.window.set_device_status("报警灯", False)
        except Exception as e:
            logger.error(f"报警灯串口打开失败: {e}", exc_info=True)
            self.window.set_device_status("报警灯", False)

        # 检测引擎（取代 TCP / Run.bat）
        try:
            self.engine.start()
            # 启动周期触发循环（默认 2s 一次，与 Halcon Run.hdev wait_seconds(2) 一致）
            loop_interval = float(self.config.get("camera.loop_interval_s", 2.0))
            if loop_interval > 0:
                self.engine.run_loop(interval_s=loop_interval)
            self.window.set_device_status("TCP", True)     # 复用 TCP 状态灯位置标记"数据源在线"
            self.window.set_device_status("Run.bat", True) # 复用 Run.bat 状态灯位置标记"相机+流水线在线"
            self.window.set_status_badge("运行中", "#27ae60")
            logger.info(f"检测引擎已启动，周期触发间隔={loop_interval}s")
        except Exception as e:
            logger.error(f"检测引擎启动失败: {e}", exc_info=True)
            self.window.set_device_status("TCP", False)
            self.window.set_device_status("Run.bat", False)
            self.window.set_status_badge("引擎异常", "#e74c3c")

        self.window.showMaximized()
        logger.info("主窗口已显示，系统就绪")

    # ─────────── 停止 ───────────
    def stop(self):
        """关闭所有服务"""
        logger.info("正在关闭所有服务...")

        try:
            self.shift_stats.update(
                self.overview_page._total,
                self.overview_page._ok_count,
                self.overview_page._ng_count,
                self.overview_page._avg_ms,
            )
        except Exception as e:
            logger.error(f"保存统计数据失败: {e}", exc_info=True)

        for name, svc, method in [
            ("检测引擎", self.engine, "stop"),
            ("飞书同步", self.feishu, "stop"),
            ("串口",     self.serial_manager, "close"),
            ("数据库",   self.database, "disconnect"),
        ]:
            try:
                getattr(svc, method)()
                logger.info(f"{name} 已关闭")
            except Exception as e:
                logger.error(f"{name} 关闭失败: {e}", exc_info=True)
        logger.info("所有服务已关闭")

    # ─────────── 班次清零 ───────────
    def _check_shift_reset(self):
        try:
            current_times = self.settings_page.get_shift_reset_times()
            self.shift_stats.reset_times = current_times
        except Exception:
            pass
        if self.shift_stats.check_and_reset():
            self.overview_page.shift_reset_signal.emit()

    # ─────────── 检测数据回调 ───────────
    def _on_inspect_data(self, data: InspectData):
        """工作线程上：仅做日志和信号转发"""
        logger.info(
            f"检测完成: rod_id={data.rod_id}, result={data.result}, "
            f"defect_type={data.defect_type}, defect_count={data.defect_count}, "
            f"ct={data.ct*1000:.0f}ms"
        )
        self._inspect_data_signal.emit(data)

    def _handle_inspect_data(self, data: InspectData):
        """UI 线程：复用与 main.py._handle_tcp_data 完全相同的消费链路"""
        try:
            self.overview_page.on_inspect_data(data)
        except Exception as e:
            logger.error(f"更新总览页面失败: {e}", exc_info=True)

        # NG 处理：串口 + 图库（弹窗放最后）
        alarm_enabled = True
        if data.result == "NG":
            try:
                if hasattr(self.overview_page, "is_alarm_enabled"):
                    alarm_enabled = self.overview_page.is_alarm_enabled()
            except Exception:
                alarm_enabled = True

            if alarm_enabled:
                try:
                    self.serial_manager.send_ng()
                except Exception as e:
                    logger.error(f"发送 NG 串口信号失败: {e}", exc_info=True)
            else:
                logger.info("报警已禁用，跳过 NG 串口信号发送")

            try:
                ts = data.timestamp or datetime.datetime.now().strftime("%H:%M:%S")
                self.gallery_page.add_defect(
                    rod_id=data.rod_id,
                    defect_type=data.defect_type or "未知",
                    timestamp=ts, image_path=None,
                )
            except Exception as e:
                logger.error(f"添加缺陷图库失败: {e}", exc_info=True)

        # 后台耗时任务（保存图 / 写库 / 上传飞书 / MES）
        from PyQt6.QtCore import QRunnable, QThreadPool

        class _BackgroundTask(QRunnable):
            def __init__(self_, d, app):
                super().__init__()
                self_.d = d
                self_.app = app

            def run(self_):
                app = self_.app
                data = self_.d

                image_path = None
                if data.image is not None and app.config.get("image_store.enabled", False):
                    image_path = app._save_image(data)
                    if image_path and data.result == "NG":
                        try:
                            app.gallery_page.update_image(data.rod_id, image_path)
                        except Exception as e:
                            logger.warning(f"更新缺陷图库图片失败: {e}")

                if app.database.is_connected:
                    try:
                        line_id = app.config.get("line_id", "PV-B02")
                        app.database.save_result(
                            rod_id=data.rod_id, result=data.result,
                            defect_type=data.defect_type,
                            defect_count=data.defect_count,
                            image_path=image_path, line_id=line_id,
                            inspect_id=data.inspect_id,
                            quality=getattr(data, 'quality', 0),
                            max_area=data.max_area,
                            total_area=data.total_area,
                            max_length=data.max_length,
                            ct=getattr(data, 'ct', 0.0),
                            check_time=getattr(data, 'check_time', ''),
                            upload_time=getattr(data, 'upload_time', ''),
                        )
                    except Exception as e:
                        logger.error(f"写入数据库失败: {e}", exc_info=True)

                if app.feishu.is_enabled:
                    try:
                        app.feishu.push_result(
                            rod_id=data.rod_id, result=data.result,
                            defect_type=data.defect_type,
                            defect_count=data.defect_count,
                            line_id=app.config.get("line_id", "PV-B02"),
                        )
                    except Exception as e:
                        logger.error(f"飞书上传失败: {e}", exc_info=True)

                if data.result == "NG" and app.http_client.is_enabled:
                    try:
                        if image_path and isinstance(data.raw_json, dict):
                            data.raw_json.setdefault("图片路径", image_path)
                        success, msg = app.http_client.upload_ng(data)
                    except Exception as e:
                        logger.error(f"MES 上传异常: {e}", exc_info=True)
                        success, msg = False, f"异常: {type(e).__name__}"
                    app._mes_status_signal.emit(success, data.rod_id or "", msg)

        task = _BackgroundTask(data, self)
        task.setAutoDelete(True)
        QThreadPool.globalInstance().start(task)

        # NG 弹窗（仅启用报警时）
        if data.result == "NG" and alarm_enabled:
            try:
                self._show_ng_popup(data)
            except Exception as e:
                logger.error(f"显示 NG 弹窗失败: {e}", exc_info=True)

    def _show_ng_popup(self, data: InspectData):
        msg = QMessageBox(self.window)
        msg.setWindowTitle("NG 报警")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText("<div style='font-size:18px;font-weight:bold;color:#e74c3c'>"
                    "检测NG棒</div>")
        detail_lines = [f"棒号：{data.rod_id or '未知'}"]
        if data.defect_type:
            detail_lines.append(f"缺陷类型：{data.defect_type}")
        if data.defect_count:
            detail_lines.append(f"缺陷数量：{data.defect_count}")
        msg.setInformativeText("\n".join(detail_lines))

        reset_btn = msg.addButton("复 位", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("关 闭", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() is reset_btn:
            self._on_reset_clicked()

    def _on_reset_clicked(self):
        logger.info("用户触发复位")
        try:
            ok = self.serial_manager.send_reset()
            if ok:
                if hasattr(self.window, "set_status_badge"):
                    self.window.set_status_badge("已复位", "#27ae60")
            else:
                QMessageBox.warning(self.window, "复位失败",
                                     "串口未打开或发送失败，请检查串口设置。")
        except Exception as e:
            logger.error(f"复位失败: {e}", exc_info=True)
            QMessageBox.critical(self.window, "复位异常", f"复位操作异常：\n{e}")

    def _on_serial_settings_changed(self):
        logger.info("串口设置已更改，重新打开串口...")
        try:
            if self.serial_manager.reload():
                self.window.set_device_status("报警灯", True)
            else:
                self.window.set_device_status("报警灯", False)
        except Exception as e:
            logger.error(f"重载串口失败: {e}", exc_info=True)

    def _on_alarm_enabled_changed(self, enabled: bool):
        try:
            self.config.set("alarm.enabled", bool(enabled))
            self.config.save()
        except Exception as e:
            logger.error(f"保存报警开关状态失败: {e}", exc_info=True)

    def _on_mes_status_updated(self, success: bool, rod_id: str, message: str):
        try:
            if hasattr(self.overview_page, "set_mes_status"):
                self.overview_page.set_mes_status(success, rod_id, message)
        except Exception as e:
            logger.error(f"更新 MES 状态标签失败: {e}", exc_info=True)

    def set_rod_id(self, rod_id: str) -> None:
        """外部（扫码枪等）注入当前棒号"""
        with self._rod_id_lock:
            self._latest_rod_id = rod_id or "NoRead"

    def _save_image(self, data: InspectData):
        try:
            base_dir = self.config.get("image_store.base_dir", "D:/SiRod/images")
            today = datetime.date.today().isoformat()
            result_dir = os.path.join(base_dir, today, data.result)
            os.makedirs(result_dir, exist_ok=True)

            ts = datetime.datetime.now().strftime("%H%M%S_%f")
            filename = f"{data.rod_id}_{ts}.png"
            filepath = os.path.join(result_dir, filename)

            from PIL import Image
            img = Image.fromarray(data.image)
            img.save(filepath)
            logger.info(f"图像已保存: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"保存图像失败: {e}", exc_info=True)
            return None

    def _update_status(self):
        """定时刷新底部状态栏"""
        # 复用 main.py 的状态灯位置（数据源/Run.bat 现在代表相机/引擎）
        self.window.set_device_status("TCP", self.engine.is_running)
        self.window.set_device_status("Run.bat", self.engine.is_looping)
        self.window.update_recv_count(self.engine.inspect_count)
        self.window.set_device_status("数据库", self.database.is_connected)
        self.window.set_device_status("报警灯", self.serial_manager.is_open)


def _global_exception_handler(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical("未捕获的异常",
                     exc_info=(exc_type, exc_value, exc_tb))


def main():
    sys.excepthook = _global_exception_handler

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)

    try:
        controller = SiRodCameraApp()
        controller.start()
    except Exception as e:
        logger.critical(f"应用启动失败: {e}", exc_info=True)
        QMessageBox.critical(None, "启动失败", f"应用启动失败:\n{e}")
        sys.exit(1)

    app.aboutToQuit.connect(controller.stop)
    logger.info("进入事件循环")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
