"""
SiRod Inspector - 光伏方棒隐裂检测系统
======================================
程序入口：初始化日志 → 配置 → 数据库 → 串口 → TCP → 飞书 → UI。
"""

import sys
import os
import datetime
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
logger = get_logger("SiRod.Main")

logger.info("=" * 60)
logger.info("SiRod Inspector 启动中...")
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
    from core.tcp_server import TCPServer, InspectData
    from core.serial_manager import SerialManager          # ← 新增
    from core.http_client import MesHttpClient             # ← 新增 MES HTTP 上传
    from core.run_bat_manager import RunBatManager         # ← 新增 Run.bat 进程管理
    from ui.styles import DARK_STYLE
    from ui.main_window import MainWindow
    from ui.overview_page import OverviewPage
    from ui.history_page import HistoryPage
    from ui.gallery_page import GalleryPage
    from ui.stats_page import StatsPage
    from ui.settings_page import SettingsPage
except ImportError as e:
    logger.critical(f"模块导入失败: {e}", exc_info=True)
    print(f"\n[FATAL] 模块导入失败: {e}")
    print("请确保已安装所有依赖: pip install PyQt6 numpy Pillow pymysql matplotlib openpyxl requests pyserial")
    sys.exit(1)


class SiRodApp(QObject):
    """应用主控制器 — 串联所有模块"""

    _tcp_data_signal = pyqtSignal(object)  # InspectData
    # MES HTTP 上传结果信号（后台线程 → UI 线程）: (success, rod_id, message)
    _mes_status_signal = pyqtSignal(bool, str, str)
    # Run.bat 状态变化信号（任意线程 → UI 线程）: running
    _run_bat_status_signal = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        logger.info("初始化应用控制器...")

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
        logger.info(f"飞书同步: {'已启用' if self.feishu.is_enabled else '未启用'}")

        # ── 串口通信（报警灯 / PLC）──
        self.serial_manager = SerialManager(self.config)
        logger.info(
            f"串口配置: enabled={self.config.get('serial.enabled', True)}, "
            f"port={self.config.get('serial.port', 'COM3')}, "
            f"baudrate={self.config.get('serial.baudrate', 9600)}"
        )

        # ── MES HTTP 上传客户端 ──
        self.http_client = MesHttpClient(self.config)
        logger.info(
            f"MES HTTP 上传: enabled={self.config.get('http.enabled', True)}, "
            f"url={self.config.get('http.url', '')}"
        )

        # ── Run.bat 子进程管理器 ──
        bat_path = os.path.join(_PROJECT_DIR, "Run.bat")
        # 状态变化通过信号转发到 UI 线程，避免直接跨线程操作 widget
        self.run_bat_manager = RunBatManager(
            bat_path=bat_path,
            on_status_changed=lambda running: self._run_bat_status_signal.emit(bool(running)),
        )
        logger.info(f"Run.bat 路径: {bat_path}")

        # TCP 服务器
        tcp_host = self.config.get("tcp.host", "127.0.0.1")
        tcp_port = self.config.get("tcp.port", 3000)
        self.tcp_server = TCPServer(host=tcp_host, port=tcp_port)
        logger.info(f"TCP 服务器配置: {tcp_host}:{tcp_port}")

        # 班次统计管理器
        reset_times = self.config.get("shift.reset_times", ["08:00", "20:00"])
        self.shift_stats = ShiftStats(
            project_dir=_PROJECT_DIR,
            reset_times=reset_times,
        )
        logger.info(f"班次清零时间: {reset_times}")

        # UI
        self.window = MainWindow()
        self.overview_page = OverviewPage()
        self.history_page = HistoryPage(database=self.database)
        self.gallery_page = GalleryPage()
        self.stats_page = StatsPage(database=self.database)
        self.settings_page = SettingsPage(config=self.config)

        self.window.add_page("overview", self.overview_page)
        self.window.add_page("history", self.history_page)
        self.window.add_page("gallery", self.gallery_page)
        self.window.add_page("stats", self.stats_page)
        self.window.add_page("settings", self.settings_page)
        logger.info("UI 页面已注册")

        self.overview_page.set_shift_stats(self.shift_stats)

        # 恢复"启用报警"勾选状态（默认 True）
        alarm_enabled = self.config.get("alarm.enabled", True)
        if hasattr(self.overview_page, "set_alarm_enabled"):
            self.overview_page.set_alarm_enabled(bool(alarm_enabled))
            logger.info(f"启用报警初始状态: {alarm_enabled}")

        # 绑定回调：TCP 线程 → 信号 → UI 线程
        self._tcp_data_signal.connect(self._handle_tcp_data)
        self.tcp_server.register_callback(self._on_tcp_data)

        # MES HTTP 上传结果：后台线程 → UI 线程
        self._mes_status_signal.connect(self._on_mes_status_updated)

        # Run.bat 状态变化：任意线程 → UI 线程
        self._run_bat_status_signal.connect(self._on_run_bat_status_updated)

        # 主界面"复位"按钮 → 发送串口复位信号
        if hasattr(self.overview_page, "reset_requested"):
            self.overview_page.reset_requested.connect(self._on_reset_clicked)
            logger.info("已连接复位按钮信号 (overview_page.reset_requested)")

        # 主界面"启用报警"勾选框 → 持久化到 config
        if hasattr(self.overview_page, "alarm_enabled_changed"):
            self.overview_page.alarm_enabled_changed.connect(
                self._on_alarm_enabled_changed
            )
            logger.info("已连接报警开关信号")

        # 设置页修改串口参数后自动重连
        if hasattr(self.settings_page, "serial_settings_changed"):
            self.settings_page.serial_settings_changed.connect(
                self._on_serial_settings_changed
            )
            logger.info("已连接串口设置变更信号")

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
                logger.info("数据库连接成功")
            else:
                self.window.set_device_status("数据库", False)
                logger.warning("数据库连接失败（非异常）")
        except Exception as e:
            logger.error(f"数据库连接异常: {e}", exc_info=True)
            self.window.set_device_status("数据库", False)

        # 飞书
        try:
            if self.feishu.is_enabled:
                self.feishu.start()
                self.window.set_device_status("飞书", True)
                logger.info("飞书同步已启动")
        except Exception as e:
            logger.error(f"飞书同步启动失败: {e}", exc_info=True)

        # 串口（报警灯）
        try:
            if self.serial_manager.open():
                self.window.set_device_status("报警灯", True)
                logger.info("报警灯串口已打开")
            else:
                self.window.set_device_status("报警灯", False)
                logger.warning("报警灯串口未打开（未启用或打开失败）")
        except Exception as e:
            logger.error(f"报警灯串口打开失败: {e}", exc_info=True)
            self.window.set_device_status("报警灯", False)

        # TCP 服务器
        try:
            self.tcp_server.start()
            self.window.set_device_status("TCP", True)
            self.window.set_status_badge("运行中", "#27ae60")
            logger.info("TCP 服务器已启动")
        except Exception as e:
            logger.error(f"TCP 服务器启动失败: {e}", exc_info=True)
            self.window.set_device_status("TCP", False)
            self.window.set_status_badge("TCP 异常", "#e74c3c")

        # Run.bat 子进程（最后启动，避免拖慢主程序就绪）
        try:
            if self.run_bat_manager.start():
                self.window.set_device_status("Run.bat", True)
                logger.info(f"Run.bat 已拉起 (pid={self.run_bat_manager.pid})")
            else:
                self.window.set_device_status("Run.bat", False)
                logger.warning("Run.bat 启动失败（文件不存在或非 Windows 平台）")
        except Exception as e:
            logger.error(f"Run.bat 启动异常: {e}", exc_info=True)
            self.window.set_device_status("Run.bat", False)

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
            logger.info("统计数据已持久化")
        except Exception as e:
            logger.error(f"保存统计数据失败: {e}", exc_info=True)

        for name, svc, method in [
            ("Run.bat", self.run_bat_manager, "stop"),       # ← 新增（放最前，先杀子进程树）
            ("TCP 服务器", self.tcp_server, "stop"),
            ("飞书同步", self.feishu, "stop"),
            ("串口", self.serial_manager, "close"),
            ("数据库", self.database, "disconnect"),
        ]:
            try:
                getattr(svc, method)()
                logger.info(f"{name} 已关闭")
            except Exception as e:
                logger.error(f"{name} 关闭失败: {e}", exc_info=True)
        logger.info("所有服务已关闭")

    # ─────────── 班次清零检查 ───────────
    def _check_shift_reset(self):
        try:
            current_times = self.settings_page.get_shift_reset_times()
            self.shift_stats.reset_times = current_times
        except Exception:
            pass

        if self.shift_stats.check_and_reset():
            self.overview_page.shift_reset_signal.emit()
            logger.info("班次清零已执行")

    # ─────────── TCP 数据回调 ───────────
    def _on_tcp_data(self, data: InspectData):
        """TCP 后台线程回调：只做日志和信号转发"""
        logger.info(
            f"收到检测数据: rod_id={data.rod_id}, result={data.result}, "
            f"defect_type={data.defect_type}, defect_count={data.defect_count}"
        )
        self._tcp_data_signal.emit(data)

    def _handle_tcp_data(self, data: InspectData):
        """UI 线程中处理 TCP 数据"""
        try:
            self.overview_page.on_inspect_data(data)
        except Exception as e:
            logger.error(f"更新总览页面失败: {e}", exc_info=True)

        # ── NG 处理：串口发信号 + 加入图库（弹窗放最后，避免阻塞）──
        alarm_enabled = True
        if data.result == "NG":
            # 检查主界面"启用报警"勾选状态
            try:
                if hasattr(self.overview_page, "is_alarm_enabled"):
                    alarm_enabled = self.overview_page.is_alarm_enabled()
            except Exception:
                alarm_enabled = True

            if alarm_enabled:
                # 1) 通过串口发送 NG 信号
                try:
                    self.serial_manager.send_ng()
                except Exception as e:
                    logger.error(f"发送 NG 串口信号失败: {e}", exc_info=True)
            else:
                logger.info("报警已禁用，跳过 NG 串口信号发送")

            # 2) 加入缺陷图库（报警开关不影响记录）
            try:
                ts = data.timestamp or datetime.datetime.now().strftime("%H:%M:%S")
                self.gallery_page.add_defect(
                    rod_id=data.rod_id,
                    defect_type=data.defect_type or "未知",
                    timestamp=ts,
                    image_path=None,
                )
            except Exception as e:
                logger.error(f"添加缺陷图库失败: {e}", exc_info=True)

        # ── 启动后台耗时任务（保存图像 / 写库 / 上传飞书 / 上传 MES）──
        # 重要：必须在弹窗之前启动，否则 QMessageBox.exec() 会阻塞 UI 线程，
        # 导致飞书/数据库/MES 都要等用户复位才会开始执行。
        from PyQt6.QtCore import QRunnable, QThreadPool

        class _BackgroundTask(QRunnable):
            def __init__(self_, d, app):
                super().__init__()
                self_.d   = d
                self_.app = app

            def run(self_):
                app   = self_.app
                data  = self_.d

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
                            rod_id=data.rod_id,
                            result=data.result,
                            defect_type=data.defect_type,
                            defect_count=data.defect_count,
                            image_path=image_path,
                            line_id=line_id,
                            inspect_id=data.inspect_id,
                            quality=getattr(data, 'quality', 0),
                            max_area=data.max_area,
                            total_area=data.total_area,
                            max_length=data.max_length,
                            ct=getattr(data, 'ct', 0.0),
                            check_time=getattr(data, 'check_time', ''),
                            upload_time=getattr(data, 'upload_time', ''),
                        )
                        logger.debug(f"数据库写入成功: {data.rod_id}")
                    except Exception as e:
                        logger.error(f"写入数据库失败: {e}", exc_info=True)

                # 飞书推送（独立于报警/弹窗，无论用户是否复位都立即执行）
                if app.feishu.is_enabled:
                    try:
                        app.feishu.push_result(
                            rod_id=data.rod_id,
                            result=data.result,
                            defect_type=data.defect_type,
                            defect_count=data.defect_count,
                            line_id=app.config.get("line_id", "PV-B02"),
                        )
                        logger.debug(f"飞书上传成功: {data.rod_id}")
                    except Exception as e:
                        logger.error(f"飞书上传失败: {e}", exc_info=True)

                # MES HTTP 上传（仅 NG 才上传）
                if data.result == "NG" and app.http_client.is_enabled:
                    try:
                        # 如果图像被保存，把本地路径补到 raw_json["图片路径"]
                        # 让 MES 请求能带上图片路径（若 TCP 原始数据未提供）
                        if image_path and isinstance(data.raw_json, dict):
                            data.raw_json.setdefault("图片路径", image_path)
                        success, msg = app.http_client.upload_ng(data)
                    except Exception as e:
                        logger.error(f"MES 上传异常: {e}", exc_info=True)
                        success, msg = False, f"异常: {type(e).__name__}"
                    # 通过信号在 UI 线程中更新主界面的状态标签
                    app._mes_status_signal.emit(success, data.rod_id or "", msg)

        task = _BackgroundTask(data, self)
        task.setAutoDelete(True)
        QThreadPool.globalInstance().start(task)

        # ── 最后再弹窗（仅 NG 且报警启用时）──
        # 弹窗会阻塞 UI 线程直到用户操作，但此时后台任务已在另一线程启动，
        # 不会被弹窗影响 — 飞书/数据库/MES 不会等待用户复位。
        if data.result == "NG" and alarm_enabled:
            try:
                self._show_ng_popup(data)
            except Exception as e:
                logger.error(f"显示 NG 弹窗失败: {e}", exc_info=True)
        elif data.result == "NG":
            logger.info("报警已禁用，跳过 NG 弹窗")

    # ─────────── NG 弹窗 ───────────
    def _show_ng_popup(self, data: InspectData):
        """NG 报警弹窗，包含复位按钮"""
        msg = QMessageBox(self.window)
        msg.setWindowTitle("NG 报警")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText(
            "<div style='font-size:18px;font-weight:bold;color:#e74c3c'>"
            "检测NG棒</div>"
        )
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

    # ─────────── 复位 ───────────
    def _on_reset_clicked(self):
        """复位按钮回调 — 发送串口复位信号"""
        logger.info("用户触发复位")
        try:
            ok = self.serial_manager.send_reset()
            if ok:
                logger.info("复位信号已发送")
                if hasattr(self.window, "set_status_badge"):
                    self.window.set_status_badge("已复位", "#27ae60")
            else:
                QMessageBox.warning(
                    self.window, "复位失败",
                    "串口未打开或发送失败，请检查串口设置。"
                )
        except Exception as e:
            logger.error(f"复位失败: {e}", exc_info=True)
            QMessageBox.critical(self.window, "复位异常", f"复位操作异常：\n{e}")

    # ─────────── 串口设置变更 ───────────
    def _on_serial_settings_changed(self):
        """设置页修改串口参数后重新打开串口"""
        logger.info("串口设置已更改，重新打开串口...")
        try:
            if self.serial_manager.reload():
                self.window.set_device_status("报警灯", True)
                logger.info("串口已按新配置重新打开")
            else:
                self.window.set_device_status("报警灯", False)
                logger.warning("串口重连失败")
        except Exception as e:
            logger.error(f"重载串口失败: {e}", exc_info=True)

    # ─────────── 报警开关 ───────────
    def _on_alarm_enabled_changed(self, enabled: bool):
        """主界面"启用报警"勾选变更 → 持久化到 config.json"""
        try:
            self.config.set("alarm.enabled", bool(enabled))
            self.config.save()
            logger.info(f"报警开关状态已保存: {enabled}")
        except Exception as e:
            logger.error(f"保存报警开关状态失败: {e}", exc_info=True)

    # ─────────── MES HTTP 上传状态 ───────────
    def _on_mes_status_updated(self, success: bool, rod_id: str, message: str):
        """后台线程的 HTTP 结果 → UI 线程更新主界面状态标签"""
        try:
            if hasattr(self.overview_page, "set_mes_status"):
                self.overview_page.set_mes_status(success, rod_id, message)
        except Exception as e:
            logger.error(f"更新 MES 状态标签失败: {e}", exc_info=True)

    # ─────────── Run.bat 状态变化 ───────────
    def _on_run_bat_status_updated(self, running: bool):
        """Run.bat 状态变化（管理器回调 → UI 线程）"""
        try:
            self.window.set_device_status("Run.bat", bool(running))
            logger.debug(f"Run.bat 状态已刷新到 UI: running={running}")
        except Exception as e:
            logger.error(f"更新 Run.bat 状态标签失败: {e}", exc_info=True)

    # ─────────── 其他 ───────────
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
        self.window.set_device_status("TCP", self.tcp_server.is_running)
        self.window.update_recv_count(self.tcp_server.total_received)
        self.window.set_device_status("数据库", self.database.is_connected)
        self.window.set_device_status("报警灯", self.serial_manager.is_open)
        # 主动 poll Run.bat 状态（poll 内部检测到状态变化会触发回调 → 信号 → UI 更新）
        running = self.run_bat_manager.poll_status()
        # 同时直接刷新一下 UI（避免回调因状态未变化而没触发）
        self.window.set_device_status("Run.bat", running)


def _global_exception_handler(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical(
        "未捕获的异常",
        exc_info=(exc_type, exc_value, exc_tb),
    )


def main():
    sys.excepthook = _global_exception_handler

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)

    try:
        controller = SiRodApp()
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
