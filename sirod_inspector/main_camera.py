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
import time
import traceback
import json

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
    from PyQt6.QtCore import QTimer, pyqtSignal, QObject, Qt
    from PyQt6.QtGui import QIcon

    from data.config import AppConfig
    from data.database import Database
    from data.feishu import FeishuSync
    from data.shift_stats import ShiftStats
    from core.tcp_server import InspectData     # 只用数据类，不启动 TCPServer
    from core.inspect_engine import InspectEngine, InspectEngineConfig
    from core.scanner_client import ScannerClient
    from core.serial_manager import SerialManager
    from core.http_client import MesHttpClient
    from ui.styles import DARK_STYLE
    from ui.main_window import MainWindow
    from ui.overview_page import OverviewPage
    from ui.history_page import HistoryPage
    from ui.gallery_page import GalleryPage
    from ui.stats_page import StatsPage
    from ui.settings_page import SettingsPage
    from ui.log_page import LogPage
    from ui.judge_page import JudgePage
    from ui.camera_page import CameraPage
    from algorithm.judge import ClassRule, DEFAULT_CLASS_RULES
    from algorithm import JudgeConfig
except ImportError as e:
    logger.critical(f"模块导入失败: {e}", exc_info=True)
    print(f"\n[FATAL] 模块导入失败: {e}")
    print("请确保已安装所有依赖: "
          "pip install PyQt6 numpy Pillow pymysql matplotlib openpyxl requests pyserial opencv-python")
    sys.exit(1)


def save_inspect_images(data: InspectData, detection_result,
                         *,
                         base_dir: str,
                         ng_trigger_classes: set = None,
                         raw_tif_dir: str = "D:/SiRod/ImageRaw",
                         web_image_dir: str = "D:/SiRod/WebImage",
                         web_url_base: str = "") -> dict:
    """把一次检测的所有图落盘。可独立调用（不依赖 SiRodCameraApp）。

    存图清单::

        <base_dir>/<date>/full/raw/<OK|NG>/<stem>.bmp        # 干净大图（训练用）
        <base_dir>/<date>/full/marked/<OK|NG>/<stem>.png     # 叠 mask 大图
        <base_dir>/<date>/crops/raw/<stem>_d<i>_<类别>.bmp   # 干净小图
        <base_dir>/<date>/crops/marked/<stem>_d<i>_<类别>.png # 叠 mask 小图

        <raw_tif_dir>/<stem>.tif                              # 原始 uint16 大图（每根棒）
        <web_image_dir>/<stem>.png                            # 给 MES 的带标注图（仅 NG）

    其中 ``stem = <棒号>_<HHMMSS_microseconds>``。

    Returns
    -------
    dict
        ``full_raw`` / ``full_marked`` / ``crop_raw`` / ``crop_marked`` /
        ``crops_count`` / ``raw_tif`` / ``web_image`` / ``web_url``。
        失败/未跑某项时 key 不会存在。
    """
    import cv2
    from sirod_inspector.algorithm import draw_marked_full, draw_marked_crop

    paths: dict = {}
    if data.image is None:
        return paths

    if ng_trigger_classes is None:
        ng_trigger_classes = {"隐裂"}

    today = datetime.date.today().isoformat()
    ts = datetime.datetime.now().strftime("%H%M%S_%f")
    # rod_id 可能含路径/Windows 非法字符（生产场景扫码枪可能给 "ABC/123" 之类）
    # 必须 sanitize 防破坏目录结构 / Windows 写盘报错
    raw_rod = data.rod_id or "NoRead"
    safe_rod = raw_rod
    for ch in r'<>:"/\|?*' + '\t\r\n\x00':
        safe_rod = safe_rod.replace(ch, "_")
    safe_rod = safe_rod.strip(". ") or "NoRead"   # Windows 不允许尾点/空格
    stem = f"{safe_rod}_{ts}"

    # 已创建目录的缓存（每次落盘 makedirs 太频繁，缓存后 30+ 次/棒 → 1-3 次）
    _dir_cache: set = set()

    def _imwrite(path, img, ext):
        """原子写：tmp 文件 + os.replace。
        防磁盘半满时半截 TIF/BMP 被下游 MES 当成训练数据/marked 图传走。
        """
        d = os.path.dirname(path)
        if d and d not in _dir_cache:
            os.makedirs(d, exist_ok=True)
            _dir_cache.add(d)
        ok, buf = cv2.imencode(ext, img)
        if not ok:
            return False
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(buf.tobytes())
            os.replace(tmp_path, path)
            return True
        except OSError as e:
            logger.warning(f"写图失败 {path}: {e}")
            # 清理 .tmp 残留
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False

    try:
        # 0. TIF 原图（uint16，对应 Halcon D:/SiRod/ImageRaw/）
        #    优先用 detection_result.raw_input_image；没有时退回 data.image
        raw_for_tif = None
        if detection_result is not None and detection_result.raw_input_image is not None:
            raw_for_tif = detection_result.raw_input_image
        if raw_for_tif is not None:
            tif_path = os.path.join(raw_tif_dir, f"{stem}.tif")
            if _imwrite(tif_path, raw_for_tif, ".tif"):
                paths['raw_tif'] = tif_path

        # 1. full / raw
        full_raw_path = os.path.join(
            base_dir, today, "full", "raw", data.result, f"{stem}.bmp")
        if _imwrite(full_raw_path, data.image, ".bmp"):
            paths['full_raw'] = full_raw_path

        # 2. full / marked — 优先复用 main_camera 已经预画好的 marked
        # （挂在 data._marked_image），避免后台 save 又画一次 100ms
        if detection_result is not None:
            marked = getattr(data, "_marked_image", None)
            if marked is None:
                marked = draw_marked_full(
                    detection_result.processed_image
                        if detection_result.processed_image is not None
                        else data.image,
                    detection_result.label_map,
                    detection_result.defects,
                    detection_result.seg_class_names,
                    ng_trigger_classes=ng_trigger_classes,
                )
            full_marked_path = os.path.join(
                base_dir, today, "full", "marked",
                data.result, f"{stem}.png")
            if _imwrite(full_marked_path, marked, ".png"):
                paths['full_marked'] = full_marked_path

        # 3. crops（每个缺陷 raw + marked，类别写在文件名里）
        if detection_result is not None and detection_result.defects:
            count = 0
            for i, d in enumerate(detection_result.defects):
                if d.crop is None:
                    continue
                cls_name = (d.class_name or "未分类").strip() or "未分类"
                safe_cls = cls_name
                for ch in r'<>:"/\|?*':
                    safe_cls = safe_cls.replace(ch, "_")

                crop_raw_path = os.path.join(
                    base_dir, today, "crops", "raw",
                    f"{stem}_d{i:02d}_{safe_cls}.bmp")
                if _imwrite(crop_raw_path, d.crop, ".bmp"):
                    paths['crop_raw'] = crop_raw_path

                marked_crop = draw_marked_crop(d.crop, d)
                crop_marked_path = os.path.join(
                    base_dir, today, "crops", "marked",
                    f"{stem}_d{i:02d}_{safe_cls}.png")
                if _imwrite(crop_marked_path, marked_crop, ".png"):
                    paths['crop_marked'] = crop_marked_path
                count += 1
            paths['crops_count'] = count

        # 4. WebImage — 仅 NG 存，给 MES 用，名字加 HTTP URL 写到 raw_json
        if data.result == "NG" and 'full_marked' in paths:
            # 复用 full/marked 那张图（同样的标注），单独放一份到 WebImage 目录
            try:
                import shutil
                web_path = os.path.join(web_image_dir, f"{stem}.png")
                d_ = os.path.dirname(web_path)
                if d_:
                    os.makedirs(d_, exist_ok=True)
                shutil.copyfile(paths['full_marked'], web_path)
                paths['web_image'] = web_path
                if web_url_base:
                    paths['web_url'] = (web_url_base.rstrip("/") + "/"
                                          + f"{stem}.png")
            except Exception as e:
                logger.warning(f"WebImage 复制失败: {e}")

        logger.info(
            f"图像已保存 ({len(paths)} 类): "
            f"tif={'✓' if 'raw_tif' in paths else '✗'} "
            f"full_raw={'✓' if 'full_raw' in paths else '✗'} "
            f"full_marked={'✓' if 'full_marked' in paths else '✗'} "
            f"crops={paths.get('crops_count', 0)} "
            f"web={'✓' if 'web_image' in paths else '✗'}"
        )
    except Exception as e:
        logger.error(f"保存图像失败: {e}", exc_info=True)
    return paths


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

        # ── Watchdog 状态（_update_status 定时刷新）──
        self._wd_last_count = 0
        self._wd_last_change_at = time.time()
        # 长时间没新检测仅显示"等待触发"轻提示，不算异常
        # （现场可能 30s 都没棒经过 — 这是正常等待）
        self._wd_idle_threshold_s = float(
            self.config.get("watchdog.idle_threshold_s", 30.0))
        # 真异常窗口：on_error 触发后 N 秒内仍标红
        self._wd_error_window_s = float(
            self.config.get("watchdog.error_window_s", 10.0))
        self._wd_last_error_at: float = 0.0
        self._wd_last_error_msg: str = ""
        self._wd_warned_scanner = False     # 防 log 刷屏

        # ── 扫码枪客户端 ──
        scanner_host = self.config.get("scanner.host", "192.168.12.56")
        scanner_port = int(self.config.get("scanner.port", 5000))
        scanner_enabled = bool(self.config.get("scanner.enabled", True))
        self.scanner = ScannerClient(
            host=scanner_host, port=scanner_port,
            poll_interval_s=float(self.config.get("scanner.poll_interval_s", 5.0)),
            recv_timeout_s=float(self.config.get("scanner.recv_timeout_s", 1.0)),
            reconnect_interval_s=float(
                self.config.get("scanner.reconnect_interval_s", 3.0)),
        ) if scanner_enabled else None
        logger.info(
            f"扫码枪: enabled={scanner_enabled}, host={scanner_host}:{scanner_port}"
        )

        # ── 检测引擎（取代原 TCPServer + Run.bat）──
        # 旧 API：NG 触发类别（仅向后兼容；config.judge.per_class 优先）
        ng_classes_cfg = self.config.get("judge.ng_trigger_classes", None)
        ng_classes = (frozenset(ng_classes_cfg)
                       if isinstance(ng_classes_cfg, list) and ng_classes_cfg
                       else None)
        # 新 API：每类独立规则（config.judge.per_class 是 list[dict]）
        per_class_cfg = self.config.get("judge.per_class", None)
        if isinstance(per_class_cfg, list) and per_class_cfg:
            class_rules = []
            for d in per_class_cfg:
                if not isinstance(d, dict) or not d.get("name"):
                    continue
                class_rules.append(ClassRule(
                    name=str(d.get("name")),
                    report_ng=bool(d.get("report_ng", False)),
                    max_area=float(d.get("max_area", 1e9)),
                    max_length=float(d.get("max_length", 1e9)),
                    max_count=int(d.get("max_count", 1_000_000)),
                    min_confidence=float(d.get("min_confidence", 0.0)),
                ))
        else:
            class_rules = None  # 让 Pipeline 走兼容路径

        # 模型路径：默认 <project>/models/，可由 config.models.seg/cls 覆盖
        default_seg = os.path.join(_PARENT_DIR, "models", "Model_seg.m")
        default_cls = os.path.join(_PARENT_DIR, "models", "Model_cls.m")
        seg_path = self.config.get("models.seg", default_seg)
        cls_path = self.config.get("models.cls", default_cls)
        # 相对路径解析到项目根目录
        if not os.path.isabs(seg_path):
            seg_path = os.path.join(_PARENT_DIR, seg_path)
        if not os.path.isabs(cls_path):
            cls_path = os.path.join(_PARENT_DIR, cls_path)

        # MultiFrame 模式：config 里 camera.acquisition_mode="MultiFrame" + camera.acquisition_frame_count=N
        # 时，相机一次软触发吐 N 帧，外部拼成 (N*height, width) 大图。
        # 默认 SingleFrame —— 兼容老配置。
        acq_mode = str(self.config.get("camera.acquisition_mode", "SingleFrame"))
        acq_frame_count = self.config.get("camera.acquisition_frame_count", None)
        if acq_frame_count is not None:
            acq_frame_count = int(acq_frame_count)
        # 对齐 Halcon BV_GrapImage:7184 wait_seconds(5)：多帧首帧后强制等待秒数
        mf_first_wait = float(self.config.get("camera.multiframe_first_wait_s", 0.0))

        engine_cfg = InspectEngineConfig(
            camera_uid=0,
            width=int(self.config.get("camera.width", 1024)),
            height=int(self.config.get("camera.height", 15000)),
            exposure_us=self.config.get("camera.exposure_us", None),
            trigger_source=self.config.get("camera.trigger_source", "Software"),
            grab_timeout_ms=int(self.config.get("camera.grab_timeout_ms", 10000)),
            acquisition_mode=acq_mode,
            acquisition_frame_count=acq_frame_count,
            multiframe_first_wait_s=mf_first_wait,
            seg_model=seg_path,
            cls_model=cls_path,
            judge_config=JudgeConfig(
                max_area=float(self.config.get("judge.max_area", 10)),
                sum_area=float(self.config.get("judge.sum_area", 10)),
                max_count=int(self.config.get("judge.max_count", 10)),
                max_length=float(self.config.get("judge.max_length", 2)),
            ),
            ng_trigger_classes=ng_classes,
            class_rules=class_rules,
            pixels_per_mm=float(self.config.get("scale.pixels_per_mm", 0.0)),
        )
        logger.info(f"模型路径: seg={seg_path}  cls={cls_path}")

        # 棒号注入 — 扫码枪可用时从扫码枪取，否则 mock
        self._manual_rod_id = "NoRead"
        self._rod_id_lock = threading.Lock()

        def _rod_id_provider() -> str:
            # ★ peek-then-confirm 模式（不在这里消费）★
            # 每次抓图前只 peek 最新棒号；trigger 成功 + on_inspect 回调里
            # 才 take_if(rod) 确认消费。这样 trigger 失败（编码器没转）时
            # 扫到的棒号不会被白白消费 — 下次 trigger 仍能用上。
            if self.scanner is not None and self.scanner.is_running:
                rod = self.scanner.current_rod_id()
                if rod and rod != "NoRead":
                    return rod
            with self._rod_id_lock:
                return self._manual_rod_id

        self.engine = InspectEngine(
            engine_cfg,
            rod_id_provider=_rod_id_provider,
            on_inspect=self._on_inspect_data,   # 工作线程
            on_error=self._on_engine_error,
        )
        logger.info(
            f"检测引擎配置: judge={engine_cfg.judge_config}, "
            f"ng_trigger_classes={set(ng_classes) if ng_classes else '(默认 隐裂)'}"
        )

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
        self.judge_page = JudgePage(config=self.config)
        self.log_page = LogPage()
        # 相机参数页：reader / applier 注入 engine 的两个方法（bound methods，
        # 调用时才执行 → 容忍此时 engine 还没 start。CameraPage 内部会处理未启动情况）
        self.camera_page = CameraPage(
            config=self.config,
            reader=self.engine.read_camera_params,
            applier=self.engine.apply_camera_params,
        )

        for name, page in [
            ("overview", self.overview_page),
            ("history",  self.history_page),
            ("gallery",  self.gallery_page),
            ("stats",    self.stats_page),
            ("settings", self.settings_page),
            ("judge",    self.judge_page),     # 顺序要跟 main_window 导航顺序对齐
            ("logs",     self.log_page),
            ("camera",   self.camera_page),
        ]:
            self.window.add_page(name, page)
        # main_camera 模式启用「参数」「日志」「相机」导航按钮
        self.window.set_tab_visible("参数", True)
        self.window.set_tab_visible("日志", True)
        self.window.set_tab_visible("相机", True)
        # 保存参数后提示重启
        try:
            self.judge_page.settings_saved.connect(self._on_judge_settings_saved)
        except Exception:
            pass
        # 相机参数保存后日志一行 + 顺手刷新状态徽章（可选）
        try:
            self.camera_page.params_saved.connect(self._on_camera_params_saved)
        except Exception:
            pass
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

        # ★ Round 5 ★ MES 重试 timer：每 60s 后台 flush 失败队列
        #   MES 失败的 NG 数据先入内存队列（cap 1000），timer 定期重试
        self._mes_retry_timer = QTimer()
        self._mes_retry_timer.timeout.connect(self._on_mes_retry_tick)
        self._mes_retry_timer.start(60_000)

        # UI 心跳：定时器每秒 tick，但 ★ Round 3 优化 ★ 默认只每 10s log 一行，
        # 检测到 freeze (tick 间隔 > 1.5s) 时立即 log 警告。
        # 比之前的"每秒一行 INFO" log 量降 10x（86400/天 → 8640/天）
        self._last_hb_tick_t = time.time()
        self._last_hb_log_t = time.time()
        self._heartbeat_timer = QTimer()
        self._heartbeat_timer.timeout.connect(self._on_ui_heartbeat_tick)
        self._heartbeat_timer.start(1000)

        # ★ Round 10 ★ OS 线程级 UI 卡顿检测器
        # Round 8 _check_thread_hangs 跑在 QTimer 上，UI 一旦 hang 它自己也跑不了。
        # 这里用 stdlib threading.Thread (daemon) 独立检测 _last_hb_tick_t 滞后情况，
        # 即便 Qt 事件循环死了也能持续 log（前提：logger 是 thread-safe 的）。
        self._ui_watchdog_stop = False
        self._ui_watchdog_thread = threading.Thread(
            target=self._ui_watchdog_loop,
            name="ui-watchdog",
            daemon=True,
        )
        self._ui_watchdog_thread.start()

        # 底部状态灯 tooltip — Round 9 后扫码枪有了独立灯，飞书灯回归原义
        try:
            tip = {
                "TCP":     "数据源/检测引擎在线状态",
                "相机":     "工业相机连接 / 采集状态（掉电掉线转红）",
                "Run.bat": "检测循环在持续触发（绿）/ 已停止（红）",
                "扫码枪":   "扫码枪 TCP 连接 + 心跳",
                "飞书":     "飞书消息同步状态",
                "数据库":   "MySQL 数据库连接",
                "报警灯":   "串口报警灯连接",
            }
            for name, t in tip.items():
                if name in self.window._status_labels:
                    self.window._status_labels[name].setToolTip(t)
        except Exception:
            pass

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

        # 扫码枪
        if self.scanner is not None:
            try:
                self.scanner.start()
            except Exception as e:
                logger.error(f"扫码枪启动失败: {e}", exc_info=True)

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

        # 排空后台 QThreadPool（每次检测的存图 / 写库 / 上传都跑在这）
        # 不排空：进程退出时可能正在写一半的图 / DB transaction 半途崩，
        # 留下损坏文件。给 5 秒上限 — 单次任务正常 <1 秒，长跑挂死兜底。
        try:
            from PyQt6.QtCore import QThreadPool
            pool = QThreadPool.globalInstance()
            pending = pool.activeThreadCount()
            if pending > 0:
                logger.info(f"排空后台任务（{pending} 个进行中）...")
                pool.waitForDone(5000)
        except Exception as e:
            logger.warning(f"排空后台任务异常（忽略继续关闭）: {e}")

        # 摘掉 LogPage 的 logging handler，避免关程序时还接日志报错
        try:
            self.log_page.detach()
        except Exception:
            pass

        for name, svc, method in [
            ("检测引擎", self.engine, "stop"),
            ("扫码枪",   self.scanner, "stop"),
            ("飞书同步", self.feishu, "stop"),
            ("串口",     self.serial_manager, "close"),
            ("数据库",   self.database, "disconnect"),
        ]:
            if svc is None:
                continue
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
    def _on_inspect_data(self, data: InspectData, detection_result=None):
        """工作线程上：日志 + 预渲染 marked 图 + 信号转发。

        ``detection_result`` 是 algorithm 层完整产物（含 label_map / crops），
        通过 InspectEngine 传过来。本地通过临时 attribute 挂到 data 上，
        让 UI 线程的 ``_handle_inspect_data`` 能拿到（不污染对外契约）。

        ⚠ marked 图（mask + bbox + PIL 中文文字）在 1024×3072 上画一次要
        50-100ms。在 UI 线程画会卡 → 这里在工作线程预生成，UI 拿到现成图
        直接显示。save 后台任务也复用同一份 marked 避免重复工作。
        """
        logger.info(
            f"检测完成: rod_id={data.rod_id}, result={data.result}, "
            f"defect_type={data.defect_type}, defect_count={data.defect_count}, "
            f"ct={data.ct*1000:.0f}ms"
        )

        # ★ peek-then-confirm 配套：trigger 成功了，确认消费 scanner 棒号 ★
        # 只在 scanner 当前最新棒号 == 本次用的棒号时才消费 reset，
        # 避免 trigger 期间扫到的下一根棒被误消费
        if (self.scanner is not None
                and data.rod_id
                and data.rod_id != "NoRead"):
            try:
                consumed = self.scanner.take_if(data.rod_id)
                if consumed:
                    logger.debug(f"scanner 棒号已消费: {data.rod_id}")
            except Exception as e:
                logger.warning(f"scanner.take_if 异常（忽略）: {e}")

        # 临时挂载完整 result（非 InspectData 正式字段，仅本进程内传递）
        data._detection_result = detection_result

        # 工作线程上预生成 marked — UI 不再扛 draw_marked_full 的 100ms
        data._marked_image = None
        if detection_result is not None and data.image is not None:
            try:
                from sirod_inspector.algorithm.overlay import draw_marked_full
                data._marked_image = draw_marked_full(
                    detection_result.processed_image
                        if detection_result.processed_image is not None
                        else data.image,
                    detection_result.label_map,
                    detection_result.defects,
                    getattr(detection_result, "seg_class_names", None),
                )
            except Exception as e:
                logger.warning(f"预生成 marked 失败（UI 退回 raw）: {e}")

        # ★ Round 1 优化 ★ 工作线程上也预生成 QImage (含 .copy())
        # UI 线程只需要 QPixmap.fromImage(qimg) (~2ms)，省去 UI 线程的
        # np.ascontiguousarray + QImage 构造 + .copy() 共 ~15-30ms
        # QImage 在任意线程都能构造，QPixmap 必须 UI 线程，所以只这一段下放
        data._marked_qimage = None
        try:
            arr_for_qimg = (data._marked_image if data._marked_image is not None
                            else data.image)
            if arr_for_qimg is not None:
                from PyQt6.QtGui import QImage
                import numpy as _np
                arr_c = _np.ascontiguousarray(arr_for_qimg)
                if arr_c.ndim == 2:
                    h, w = arr_c.shape
                    qfmt = QImage.Format.Format_Grayscale8
                    bpl = w
                elif arr_c.ndim == 3 and arr_c.shape[2] == 3:
                    h, w, _ = arr_c.shape
                    qfmt = QImage.Format.Format_BGR888
                    bpl = 3 * w
                elif arr_c.ndim == 3 and arr_c.shape[2] == 4:
                    h, w, _ = arr_c.shape
                    qfmt = QImage.Format.Format_RGBA8888
                    bpl = 4 * w
                else:
                    raise ValueError(f"unsupported shape {arr_c.shape}")
                # .copy() 让 Qt 持有独立内存，跟 numpy 解耦（修原 segfault 风险）
                data._marked_qimage = QImage(
                    arr_c.data, w, h, bpl, qfmt).copy()
        except Exception as e:
            logger.warning(f"预生成 QImage 失败（UI 退回 numpy 路径）: {e}")

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
                # ★ Round 2 优化 ★ 串口写异步化 — pyserial timeout=1 最长卡 UI 1s
                self._fire_serial_async("send_ng")
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
                    paths = app._save_images(data, getattr(data, '_detection_result', None))
                    # 数据库 / 图库用 marked 大图本地路径
                    image_path = paths.get('full_marked') or paths.get('full_raw')
                    # MES 上传 用 WebImage 的 HTTP URL（仅 NG 才有）
                    web_url = paths.get('web_url')
                    if web_url and isinstance(data.raw_json, dict):
                        data.raw_json['图片路径'] = web_url
                    if image_path and data.result == "NG":
                        try:
                            # ★ Round 6 ★ bg 线程预生成 thumbnail（复用 round 1
                            # 已经做好的 _marked_qimage scale 一下），UI 线程只 fromImage
                            thumb = None
                            marked_qimg = getattr(data, "_marked_qimage", None)
                            if marked_qimg is not None:
                                try:
                                    from PyQt6.QtCore import Qt as _Qt
                                    thumb = marked_qimg.scaled(
                                        208, 170,
                                        _Qt.AspectRatioMode.KeepAspectRatio,
                                        _Qt.TransformationMode.SmoothTransformation,
                                    )
                                except Exception:
                                    thumb = None
                            app.gallery_page.update_image(
                                data.rod_id, image_path, thumb)
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
                            defects_json=json.dumps(
                                data.raw_json.get("defects", [])
                                if isinstance(data.raw_json, dict) else [],
                                ensure_ascii=False,
                            ),
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
                    # ★ Round 5 ★ 推 MES 失败 → 入重试队列，后台 QTimer 定期 flush
                    if not success:
                        try:
                            app.http_client.enqueue_retry(data)
                        except Exception as e:
                            logger.warning(f"MES 重试入队失败（忽略）: {e}")
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
        """NG 报警弹窗 — **非阻塞、单实例**

        历史踩坑：
          - 直接 ``msg.exec()`` modal 阻塞 + 多 NG 堆叠 → UI 卡死
          - ``msg.show()`` + 每次 append informativeText 累积 → 文本/重排 race UI 卡

        现版本策略（单实例）：
          - 只维护**一个** popup 引用 ``self._ng_popup``
          - 新 NG 来时：
            * popup 不存在 / 已关闭 → 新建并 ``show()``（非阻塞）
            * popup 还在屏幕上 → **只刷新文本**，不再 new 一个，不累积
          - 复位按钮 → 发串口 reset + 关 popup
          - 关闭按钮 → 关 popup
          - 顶部状态徽章 + 串口报警灯 + 图库继续照旧
        """
        # 1) 更新状态徽章（红底，operator 一目了然）
        try:
            badge_text = f"NG: {data.rod_id or 'NoRead'}"
            if data.defect_type:
                badge_text += f" / {data.defect_type}"
            if hasattr(self.window, "set_status_badge"):
                self.window.set_status_badge(badge_text, "#e74c3c")
        except Exception as e:
            logger.warning(f"更新 NG 状态徽章失败: {e}")

        # 2) 准备弹窗内容
        detail_lines = [f"棒号：{data.rod_id or '未知'}"]
        if data.defect_type:
            detail_lines.append(f"缺陷类型：{data.defect_type}")
        if data.defect_count:
            detail_lines.append(f"缺陷数量：{data.defect_count}")
        if data.max_length:
            detail_lines.append(f"最大长度：{data.max_length:.1f}")
        informative = "\n".join(detail_lines)

        # 3) 单实例：popup 还在就刷新文本，不再 new
        existing = getattr(self, "_ng_popup", None)
        if existing is not None and existing.isVisible():
            try:
                existing.setInformativeText(informative)
                # 提到最前
                existing.raise_()
                existing.activateWindow()
                return
            except Exception:
                # popup 状态异常，重建
                try:
                    existing.close()
                except Exception:
                    pass

        # 4) 新建 popup（非阻塞 show，不是 exec）
        try:
            from PyQt6.QtWidgets import QMessageBox
            msg = QMessageBox(self.window)
            msg.setWindowTitle("NG 报警")
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setText(
                "<div style='font-size:18px;font-weight:bold;color:#e74c3c'>"
                "检测到 NG 棒</div>"
            )
            msg.setInformativeText(informative)
            reset_btn = msg.addButton("复 位", QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("关 闭", QMessageBox.ButtonRole.RejectRole)

            # 按钮回调通过 finished 信号处理（非阻塞）
            def _on_finished(_code):
                try:
                    if msg.clickedButton() is reset_btn:
                        self._on_reset_clicked()
                except Exception as e:
                    logger.warning(f"复位按钮回调异常: {e}")
                # 清掉单例引用
                self._ng_popup = None
            msg.finished.connect(_on_finished)

            self._ng_popup = msg
            msg.show()    # ★ 非阻塞 ★
        except Exception as e:
            logger.error(f"创建 NG 弹窗失败: {e}", exc_info=True)

    def _on_reset_clicked(self):
        """处理复位请求 — 错误反馈走状态栏 / log，不再用 modal popup。

        历史 bug：失败时 QMessageBox.warning(...) 是 modal exec()，被
        NG popup 内的"复位"按钮触发后立刻把 UI 阻塞 → 用户点任意位置
        都未响应（因为 modal popup 拦截所有输入，且 nested 在另一个
        popup 的 finished 信号回调里）。
        """
        logger.info("用户触发复位")
        # ★ Round 2 优化 ★ 串口写异步化，回调里更新 UI 状态
        self._fire_serial_async("send_reset", on_done=self._on_reset_done)

    def _on_reset_done(self, ok: bool, err: str) -> None:
        """串口 send_reset 后台执行完，回主线程刷状态"""
        if err:
            logger.error(f"复位异常: {err}")
            if hasattr(self.window, "set_status_badge"):
                self.window.set_status_badge(f"复位异常: {err[:30]}", "#e74c3c")
        elif ok:
            if hasattr(self.window, "set_status_badge"):
                self.window.set_status_badge("已复位", "#27ae60")
        else:
            logger.warning("复位失败：串口未打开或发送失败")
            if hasattr(self.window, "set_status_badge"):
                self.window.set_status_badge(
                    "复位失败 — 检查串口", "#e74c3c")

    def _on_mes_retry_tick(self) -> None:
        """MES 重试 timer fire 后扔后台 QThreadPool 跑 flush（避免 UI 卡）"""
        if not self.http_client.is_enabled:
            return
        if self.http_client.retry_queue_size() == 0:
            return
        from PyQt6.QtCore import QRunnable, QThreadPool

        client = self.http_client

        class _RetryTask(QRunnable):
            def run(self):
                try:
                    client.flush_retries(max_per_run=10)
                except Exception as e:
                    logger.error(f"MES 重试 flush 异常: {e}", exc_info=True)

        t = _RetryTask()
        t.setAutoDelete(True)
        QThreadPool.globalInstance().start(t)

    def _on_ui_heartbeat_tick(self) -> None:
        """UI 心跳每秒 tick；正常 10s log 一次，freeze 时立即报警

        诊断逻辑：
        - QTimer 在 UI 线程上 fire，UI 卡住时 tick 也卡
        - 比对"上次 tick 时间" → 当前 tick 时间，如果 > 1.5s 说明被阻塞过
        - freeze 时立即打 WARNING（带阻塞时长），方便事后查 log 定位
        - 正常情况下每 10s 打一次 INFO，降低 log 量
        """
        now = time.time()
        gap = now - self._last_hb_tick_t
        self._last_hb_tick_t = now
        if gap > 1.5:
            # UI 主线程刚才被阻塞了，立即记录
            logger.warning(
                f"[UI_HB] {now:.1f} ⚠ 主线程上次 tick 距今 {gap:.1f}s（>1.5s 视为卡顿）"
            )
            self._last_hb_log_t = now
            return
        if now - self._last_hb_log_t >= 10.0:
            logger.info(f"[UI_HB] {now:.1f}")
            self._last_hb_log_t = now

    def _fire_serial_async(self, method_name: str, on_done=None) -> None:
        """通用：把 serial_manager.send_ng / send_reset 等扔后台执行。

        Parameters
        ----------
        method_name : "send_ng" / "send_reset" / "send_raw" 等
        on_done : Optional[Callable[[bool, str], None]]
            可选回调：在 UI 线程接收 (ok: bool, err_str: str)。
            None = fire-and-forget。
        """
        from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

        class _Sig(QObject):
            done = pyqtSignal(bool, str)

        sm = self.serial_manager

        class _Task(QRunnable):
            def __init__(self_):
                super().__init__()
                self_.signals = _Sig()

            def run(self_):
                try:
                    fn = getattr(sm, method_name)
                    result = fn()
                    ok = bool(result) if result is not None else True
                    self_.signals.done.emit(ok, "")
                except Exception as e:
                    logger.error(
                        f"串口异步 {method_name} 异常: {e}", exc_info=True)
                    self_.signals.done.emit(False, f"{type(e).__name__}: {e}")

        task = _Task()
        if on_done is not None:
            task.signals.done.connect(on_done)
        task.setAutoDelete(True)
        QThreadPool.globalInstance().start(task)

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

    def _on_judge_settings_saved(self):
        """UI 保存判定参数 → 重读 config → 热推到运行中的 Pipeline，无需重启"""
        try:
            # per-class 规则
            per_class_cfg = self.config.get("judge.per_class", None)
            new_rules = []
            if isinstance(per_class_cfg, list) and per_class_cfg:
                for d in per_class_cfg:
                    if not isinstance(d, dict) or not d.get("name"):
                        continue
                    new_rules.append(ClassRule(
                        name=str(d.get("name")),
                        report_ng=bool(d.get("report_ng", False)),
                        max_area=float(d.get("max_area", 1e9)),
                        max_length=float(d.get("max_length", 1e9)),
                        max_count=int(d.get("max_count", 1_000_000)),
                        min_confidence=float(d.get("min_confidence", 0.0)),
                    ))
            self.engine.set_class_rules(new_rules)

            # 全局几何阈值
            from sirod_inspector.algorithm import JudgeConfig
            self.engine.set_judge_config(JudgeConfig(
                max_area=float(self.config.get("judge.max_area", 10)),
                sum_area=float(self.config.get("judge.sum_area", 10)),
                max_count=int(self.config.get("judge.max_count", 10)),
                max_length=float(self.config.get("judge.max_length", 2)),
            ))
            logger.info(f"判定参数已热更新生效（{len(new_rules)} 类 + 全局阈值）")
        except Exception as e:
            logger.error(f"判定参数热更新失败，请重启程序: {e}", exc_info=True)

    def _on_camera_params_saved(self, params: dict):
        """相机参数 UI 已保存（已热更新到硬件 + 已写 config.json）。
        这里只做轻量收尾：log + 状态徽章刷新。具体写硬件 / 写 config 都由 CameraPage 内部完成。"""
        try:
            editable_keys = ("width", "height", "acquisition_mode",
                             "acquisition_frame_count", "trigger_mode",
                             "trigger_source", "exposure_us",
                             "multiframe_first_wait_s", "grab_timeout_ms")
            summary = ", ".join(
                f"{k}={params.get(k)}" for k in editable_keys
                if k in params
            )
            logger.info(f"相机参数已保存并热更新: {summary}")
            if hasattr(self.window, "set_status_badge"):
                self.window.set_status_badge("相机参数已更新", "#00d4ff")
        except Exception as e:
            logger.error(f"相机参数保存回调异常: {e}", exc_info=True)

    def _on_mes_status_updated(self, success: bool, rod_id: str, message: str):
        try:
            if hasattr(self.overview_page, "set_mes_status"):
                self.overview_page.set_mes_status(success, rod_id, message)
        except Exception as e:
            logger.error(f"更新 MES 状态标签失败: {e}", exc_info=True)

    def set_rod_id(self, rod_id: str) -> None:
        """外部手动注入棒号（在扫码枪未启用 / 离线时使用）"""
        with self._rod_id_lock:
            self._manual_rod_id = rod_id or "NoRead"

    def _save_images(self, data: InspectData, detection_result) -> dict:
        """存全部图（包装函数 — 真正逻辑在 module-level ``save_inspect_images``）

        相对路径基于项目根目录 resolve（防 cwd 被切到 EasyLabel/DeepLearning
        后相对路径写到错位置）。
        """
        def _abs_path(p: str) -> str:
            if not p:
                return p
            if os.path.isabs(p):
                return p
            return os.path.join(_PARENT_DIR, p)

        base_dir = _abs_path(
            self.config.get("image_store.base_dir", "D:/SiRod/images"))
        raw_tif_dir = _abs_path(
            self.config.get("image_store.raw_tif_dir", "D:/SiRod/ImageRaw"))
        web_image_dir = _abs_path(
            self.config.get("image_store.web_image_dir", "D:/SiRod/WebImage"))
        web_url_base = self.config.get("image_store.web_url_base",
                                         "http://10.32.50.220:8080")
        ng_cls_cfg = self.config.get("judge.ng_trigger_classes", None)
        ng_set = (set(ng_cls_cfg)
                   if isinstance(ng_cls_cfg, list) and ng_cls_cfg
                   else {"隐裂"})
        return save_inspect_images(
            data, detection_result,
            base_dir=base_dir,
            ng_trigger_classes=ng_set,
            raw_tif_dir=raw_tif_dir,
            web_image_dir=web_image_dir,
            web_url_base=web_url_base,
        )

    # 保留旧名兼容：内部仍用 _save_images
    def _save_image(self, data: InspectData):
        """兼容名：等价 ``_save_images(data, None)``，只存大图 raw"""
        paths = self._save_images(data, None)
        return paths.get('full_marked') or paths.get('full_raw')

    def _on_engine_error(self, e: Exception) -> None:
        """InspectEngine 工作线程上报异常时调用"""
        self._wd_last_error_at = time.time()
        self._wd_last_error_msg = f"{type(e).__name__}: {e}"[:80]
        logger.error(f"InspectEngine 异常: {e}", exc_info=False)

    def _update_status(self):
        """定时刷新底部状态栏 + watchdog 检查。

        状态徽章优先级（高到低）::

            红:  引擎离线 / 检测异常（近 N 秒有 on_error）
            橙:  检测循环已停 / 扫码枪离线
            蓝:  等待触发（正常空闲）
            绿:  运行中
        """
        engine_running = self.engine.is_running
        engine_looping = self.engine.is_looping
        self.window.set_device_status("TCP", engine_running)
        self.window.set_device_status("Run.bat", engine_looping)
        self.window.update_recv_count(self.engine.inspect_count)
        # 相机灯：engine 健康标志（open/grab 成功为绿，掉电掉线 grab 失败转红）
        self.window.set_device_status("相机", self.engine.is_camera_ok)
        self.window.set_device_status("数据库", self.database.is_connected)
        self.window.set_device_status("报警灯", self.serial_manager.is_open)
        scanner_ok = (self.scanner is not None
                       and self.scanner.is_connected)
        # ★ Round 9 ★ 之前误贴在"飞书"灯，命名误导。现在用专门的"扫码枪"灯
        self.window.set_device_status("扫码枪", scanner_ok)
        # 飞书状态 = config 启用 (我们不本地推飞书，只是状态显示)
        feishu_enabled = self.feishu.is_enabled if self.feishu else False
        self.window.set_device_status("飞书", feishu_enabled)

        now = time.time()
        current_count = self.engine.inspect_count
        if current_count != self._wd_last_count:
            self._wd_last_count = current_count
            self._wd_last_change_at = now

        silent_s = now - self._wd_last_change_at
        error_age = now - self._wd_last_error_at if self._wd_last_error_at else 9e9
        recent_error = error_age < self._wd_error_window_s

        # 强提示（异常）
        if not engine_running:
            self.window.set_status_badge("引擎离线", "#e74c3c")
        elif recent_error:
            self.window.set_status_badge(
                f"检测异常 {int(error_age)}s 前", "#e74c3c")
        elif not engine_looping:
            self.window.set_status_badge("检测循环已停", "#e67e22")
        elif self.scanner is not None and not scanner_ok:
            self.window.set_status_badge("扫码枪离线", "#e67e22")
            if not self._wd_warned_scanner:
                logger.warning(
                    f"扫码枪 {self.scanner.host}:{self.scanner.port} 未连接"
                )
                self._wd_warned_scanner = True
        # 轻提示（正常状态）
        elif silent_s > self._wd_idle_threshold_s:
            self.window.set_status_badge(
                f"等待触发 {int(silent_s)}s", "#3498db")
            # 不 log，空闲是正常状态
        else:
            self.window.set_status_badge("运行中", "#27ae60")
            self._wd_warned_scanner = False

        # ★ Round 8 ★ 进程级 watchdog — 检测 worker / scanner 线程 hang
        # 阈值：worker 5 分钟无 iter tick = hang；scanner 3 分钟无 heartbeat = hang
        self._check_thread_hangs(now)

    def _check_thread_hangs(self, now: float) -> None:
        """检测 worker / scanner 后台线程是否 hang，hang 时 CRITICAL log。

        阈值偏宽（5min / 3min）是为了避开偶发慢 grab + 编码器停转等正常情况。
        只 log，不 auto-restart —— 工业系统让人决定再操作，避免 cascading failure。
        """
        WORKER_HANG_THRESHOLD_S = 5 * 60   # worker iter 5 分钟没动 = hang
        SCANNER_HANG_THRESHOLD_S = 3 * 60  # scanner heartbeat 3 分钟没出 = hang
        WARN_REPEAT_INTERVAL_S = 5 * 60    # 同一警告 5 分钟不重复 log（避免刷屏）

        # worker hang 检测
        try:
            last_tick = getattr(self.engine, "_last_loop_tick_t", 0.0)
            if last_tick > 0 and self.engine.is_looping:
                gap = now - last_tick
                if gap > WORKER_HANG_THRESHOLD_S:
                    last_warn = getattr(self, "_wd_last_worker_warn_t", 0.0)
                    if now - last_warn > WARN_REPEAT_INTERVAL_S:
                        logger.critical(
                            f"[watchdog] 检测引擎 worker 可能 hang："
                            f"已 {int(gap)}s 未 iter（阈值 {WORKER_HANG_THRESHOLD_S}s）。"
                            f"可能在 BVCAM SDK 中卡死，建议重启程序。"
                        )
                        self._wd_last_worker_warn_t = now
        except Exception as e:
            logger.debug(f"_check_thread_hangs worker 检测异常: {e}")

        # scanner hang 检测
        try:
            if self.scanner is not None and self.scanner.is_running:
                last_hb = getattr(self.scanner, "_last_heartbeat_t", 0.0)
                if last_hb > 0:
                    # 注意：scanner 用 time.monotonic()，不是 time.time()
                    import time as _t
                    gap = _t.monotonic() - last_hb
                    if gap > SCANNER_HANG_THRESHOLD_S:
                        last_warn = getattr(self, "_wd_last_scanner_warn_t", 0.0)
                        if now - last_warn > WARN_REPEAT_INTERVAL_S:
                            logger.critical(
                                f"[watchdog] 扫码枪 worker 可能 hang："
                                f"已 {int(gap)}s 无心跳（阈值 {SCANNER_HANG_THRESHOLD_S}s）。"
                            )
                            self._wd_last_scanner_warn_t = now
        except Exception as e:
            logger.debug(f"_check_thread_hangs scanner 检测异常: {e}")

    def _ui_watchdog_loop(self) -> None:
        """OS 线程级 UI hang 检测器 —— 独立于 Qt 事件循环。

        Round 8 加的 _check_thread_hangs 由 QTimer 驱动，跑在 UI 线程上。
        UI 线程一旦 hang，所有 QTimer 都停摆，那个 watchdog 自己也失效。

        这里在 __init__ 末尾起一个 daemon threading.Thread，每 5s 醒一次：
          - 读 self._last_hb_tick_t（UI 心跳 Qt timer 最后一次 fire 的时间）
          - 当前 wall clock - last_tick > 10s 就视为 UI 卡死
          - CRITICAL log（logger 是 thread-safe 的，可以从任何线程写）
          - 5min 内不重复刷屏

        注意：进程退出时不显式 join —— daemon 线程会随主进程一起结束。
        """
        UI_HANG_THRESHOLD_S = 10.0
        WARN_REPEAT_INTERVAL_S = 5 * 60.0
        CHECK_INTERVAL_S = 5.0
        last_warn_t = 0.0
        while not self._ui_watchdog_stop:
            try:
                time.sleep(CHECK_INTERVAL_S)
                now = time.time()
                last_tick = self._last_hb_tick_t
                gap = now - last_tick
                if gap > UI_HANG_THRESHOLD_S:
                    if now - last_warn_t > WARN_REPEAT_INTERVAL_S:
                        logger.critical(
                            f"[ui-watchdog] UI 主线程可能 hang："
                            f"心跳 tick 距今 {gap:.1f}s（阈值 {UI_HANG_THRESHOLD_S}s）。"
                            f"如持续超过 60s，建议直接 kill 进程让 vbs supervisor 重启。"
                        )
                        last_warn_t = now
            except Exception as e:
                try:
                    logger.debug(f"ui-watchdog 异常: {e}")
                except Exception:
                    pass


def _global_exception_handler(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical("未捕获的异常",
                     exc_info=(exc_type, exc_value, exc_tb))


def _acquire_single_instance_lock():
    """单实例锁 — 防多开。

    多个 main_camera 同时跑会撞：
      - 同时 chdir 到 EasyLabel/DeepLearning（cwd 是进程全局）
      - 同时加载 dnninfer.dll / BVCam.dll（DLL 状态全局）
      - 同时写 D:/SiRod 同一份文件名
      - 同时 append 同一个 log
      - 同时抢同一相机 USB / GigE 资源
    任何一项 race 都能导致 UI 阻塞或数据损坏。

    Windows 用 msvcrt.locking 锁一个 lockfile；POSIX 用 fcntl.flock。
    """
    lock_path = os.path.join(_log_dir, "main_camera.lock")
    os.makedirs(_log_dir, exist_ok=True)

    def _try_acquire():
        # "a+" 模式：文件存在则追加，不存在则创建。比 "w" 不会因为
        # Windows 文件元数据未清完报 EACCES。
        fh = open(lock_path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            fh.close()
            raise
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()}\n")
        fh.flush()
        return fh

    # 主路径
    try:
        return _try_acquire()
    except (OSError, IOError) as e1:
        # 可能是"幽灵 lock 文件"（前一进程强杀后 OS 释放了锁但文件残留 +
        # Windows 文件 metadata 还没清）。尝试删掉再试一次。
        try:
            os.remove(lock_path)
        except OSError:
            # 删不掉 = 真有进程持有，确实是多开
            raise RuntimeError(
                f"已有一个 SiRod Inspector 实例在跑（锁文件 {lock_path}）。\n"
                f"请先关掉旧实例（任务管理器找 python）再启动。\n"
                f"底层错误: {e1}"
            )
        # 删成功 → 重试 acquire
        try:
            return _try_acquire()
        except (OSError, IOError) as e2:
            raise RuntimeError(
                f"无法获取单实例锁（{lock_path}）。\n"
                f"底层错误: {e2}"
            )


def main():
    sys.excepthook = _global_exception_handler

    # 单实例锁 — 必须在 QApplication 之前（防止重复启 PyQt）
    try:
        _lock_fh = _acquire_single_instance_lock()
    except RuntimeError as e:
        logger.error(str(e))
        # 弹一个最小窗口提示用户
        try:
            tmp_app = QApplication(sys.argv)
            QMessageBox.critical(None, "已有实例在跑", str(e))
        except Exception:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)

    # ★ Round 7 ★ 把 QThreadPool 最大并发数从默认 (CPU 核数, 通常 4-8)
    # 提到 16。后台任务多（每根棒触发 1 个 BG task, MES 重试, async serial,
    # camera read/apply, DB query, MES retry, ...），burst 时不至于排队。
    try:
        from PyQt6.QtCore import QThreadPool as _QTP
        _QTP.globalInstance().setMaxThreadCount(16)
        logger.info(
            f"QThreadPool max threads = "
            f"{_QTP.globalInstance().maxThreadCount()}"
        )
    except Exception as e:
        logger.warning(f"设置 QThreadPool 容量失败: {e}")

    # ★ Round 7 ★ 启动 splash screen — UI 起来前给操作员看的反馈
    # init 耗时 5-10s (相机 + AI 模型 + DB)，无 splash 时 vbs 启动后用户
    # 啥都看不到，体验差。
    splash = None
    try:
        from PyQt6.QtWidgets import QSplashScreen
        from PyQt6.QtGui import QPixmap as _QPx
        from PyQt6.QtCore import Qt as _Qt
        splash_pix = _QPx(500, 200)
        splash_pix.fill(_Qt.GlobalColor.darkBlue)
        splash = QSplashScreen(splash_pix,
                                _Qt.WindowType.WindowStaysOnTopHint)
        splash.showMessage(
            "SiRod Inspector\n\n正在启动 (相机 + AI 模型加载)...",
            _Qt.AlignmentFlag.AlignCenter,
            _Qt.GlobalColor.white,
        )
        splash.show()
        app.processEvents()
    except Exception as e:
        logger.warning(f"splash screen 创建失败（忽略）: {e}")
        splash = None

    try:
        controller = SiRodCameraApp()
        controller.start()
    except Exception as e:
        if splash is not None:
            try: splash.close()
            except Exception: pass
        logger.critical(f"应用启动失败: {e}", exc_info=True)
        QMessageBox.critical(None, "启动失败", f"应用启动失败:\n{e}")
        sys.exit(1)

    # splash → 关掉，把 focus 转给主窗口
    if splash is not None:
        try:
            splash.finish(controller.window)
        except Exception:
            pass

    # ────── 关闭兜底 watchdog ──────
    # 问题背景：controller.stop() 走到 engine._cleanup_partial() → BVCamera.close()
    # → BVCAM_Close C SDK 调用，偶发在内部等 pending callback 卡死，Python 控制不了，
    # 表现就是关窗后 PowerShell 终端永不退。
    # 解法：aboutToQuit 触发时，起一个 daemon 线程，hard_exit_after_s 秒后无条件
    # os._exit(0)。正常关闭快（<5s），watchdog 不会触发；卡死时强制吐出进程。
    def _kick_watchdog(hard_exit_after_s: float = 15.0) -> None:
        def _force_exit():
            time.sleep(hard_exit_after_s)
            logger.warning(
                f"[watchdog] stop() 超过 {hard_exit_after_s}s 未完成 → "
                f"os._exit(0) 强制退出"
            )
            os._exit(0)   # 注意是 _exit (下划线)，不是 exit；后者不存在于 os
        threading.Thread(target=_force_exit, daemon=True,
                         name="ShutdownWatchdog").start()

    app.aboutToQuit.connect(_kick_watchdog)
    app.aboutToQuit.connect(controller.stop)
    logger.info("进入事件循环")
    try:
        rc = app.exec()
    finally:
        # 显式持引用到 main 结束 — 避免 lock_fh 被 GC 提前释放
        del _lock_fh
    # 正常路径（stop 顺利返回）→ 主动 os._exit 兜底，防止某些 lingering 线程拖着
    os._exit(rc)


if __name__ == "__main__":
    main()
