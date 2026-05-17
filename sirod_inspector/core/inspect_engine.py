"""
检测引擎
========
把 BVCamera + algorithm.Pipeline + 棒号注入 串成一个统一的工作引擎：

    [外部触发]              [外部输入]
       │                       │
       ▼                       ▼
    Engine.trigger_once()  rod_id_provider() — 扫码枪/手动输入/PLC 信号
       │
       ▼
    Camera 软触发 ──► 抓帧 (uint16, 15000×1024)
                                │
                                ▼
                          algorithm.Pipeline
                                │
                                ▼
                       DetectionResult
                                │
                                ▼  (装配)
                          InspectData
                                │
                                ▼
                       on_inspect(data) 回调
                                │
                                ▼
                  既有 UI / DB / 飞书 / MES 消费链路

设计要点
--------
- ``trigger_once()`` 同步模式：阻塞执行一次完整流水线，返回 InspectData。
- ``run_loop()`` 异步模式：后台线程按外部触发器节奏跑（默认每 N 秒）。
- 回调 ``on_inspect(InspectData)`` 在工作线程上调用 — UI 层负责切回 UI 线程。
- 与 UI 现有数据契约完全兼容：直接产 ``InspectData`` 给现有消费者。

使用示例::

    engine = InspectEngine(
        seg_model='models/Model_seg.m',
        cls_model='models/Model_cls.m',
        rod_id_provider=lambda: scanner.read_or_wait(),
        on_inspect=lambda d: ui_signal.emit(d),
    )
    engine.start()                      # 打开相机 + 加载模型
    data = engine.trigger_once()        # 外部手动触发一次
    engine.run_loop(interval_s=2.0)     # 或后台周期触发
    engine.stop()                        # 关闭
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from sirod_inspector.algorithm import (
    DetectionResult,
    JudgeConfig,
    Pipeline,
)
from sirod_inspector.camera import BVCamera, BVCameraError
from sirod_inspector.core.tcp_server import InspectData

logger = logging.getLogger("SiRod.InspectEngine")


# ============================================================
# 工厂函数：DetectionResult → InspectData
# ============================================================

def detection_to_inspect_data(
    result: DetectionResult,
    *,
    rod_id: str = "",
    inspect_id: int = 0,
    raw_frame: Optional[np.ndarray] = None,
) -> InspectData:
    """把 algorithm 层的 DetectionResult 装配为 UI 层期望的 InspectData。

    字段映射::

        DetectionResult                  InspectData
        ─────────────────                ─────────────
        result                       →   result
        quality                      →   quality
        defect_type                  →   defect_type
        defect_count                 →   defect_count
        max_area                     →   max_area
        sum_area                     →   total_area
        max_length                   →   max_length
        processed_image / raw_frame  →   image
        ct_ms / 1000                 →   ct

    其他字段（时间戳、inspect_id）由本函数填充。

    Parameters
    ----------
    raw_frame : np.ndarray | None
        若传入，会替代 ``result.processed_image`` 作为 ``InspectData.image``。
        生产环境推荐传预处理后图（更紧凑、可直接存档）。
    """
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    image = raw_frame if raw_frame is not None else result.processed_image

    return InspectData(
        rod_id=rod_id,
        result=result.result,
        image=image,
        defect_type=result.defect_type,
        defect_count=result.defect_count,
        max_area=float(result.max_area),
        total_area=float(result.sum_area),
        max_length=float(result.max_length),
        inspect_id=inspect_id,
        quality=int(result.quality),
        ct=float(result.ct_ms) / 1000.0,
        check_time=timestamp,
        upload_time=timestamp,
        timestamp=timestamp,
        raw_json={
            "judge_reasons": list(result.judge_reasons),
            "defects": [
                {
                    "bbox": d.bbox,
                    "area": d.area,
                    "outer_radius": d.outer_radius,
                    "class_name": d.class_name,
                    "class_confidence": d.class_confidence,
                }
                for d in result.defects
            ],
        },
    )


# ============================================================
# 配置
# ============================================================

@dataclass
class InspectEngineConfig:
    """检测引擎运行参数"""
    # 相机
    camera_uid: int = 0                     # 0 = 第一台
    width: int = 1024
    height: int = 15000
    exposure_us: Optional[float] = None     # None = 沿用相机当前值
    trigger_source: str = "Software"
    grab_timeout_ms: int = 10000

    # 模型
    seg_model: str = "models/Model_seg.m"
    cls_model: str = "models/Model_cls.m"
    judge_config: JudgeConfig = field(default_factory=JudgeConfig)
    ng_trigger_classes: Optional[frozenset] = None
    """旧 API（向后兼容）。如同时设 ``class_rules``，``class_rules`` 优先。"""

    class_rules: Optional[list] = None
    """新 API：每类独立 5 字段判定规则。``None`` 时由
       ``ng_trigger_classes`` 兼容构造（默认仅"隐裂"算 NG）。
       配置入口：``config.json`` 的 ``judge.per_class`` list[dict]。"""

    # 行为
    use_preprocessed_as_inspect_image: bool = True
    """``True``: ``InspectData.image`` 用 1024×3072 预处理后图（推荐，跟下游存档/上传一致）；
       ``False``: 用原始 15000×1024 uint16 图（大、占内存，仅用于训练数据采集）。"""

    # NOTE: skip_preprocess 字段已移除（dead code）。
    # Pipeline.process() 内部按 image dtype 自动判别：
    #   uint16 → 跑 preprocess；uint8 → 跳过。这个判别已经足够，
    #   再额外引入开关只会让调用方困惑。如果未来真需要强制路径，
    #   建议改在 Pipeline 层加参数，而不是在 Engine 层。


# ============================================================
# 引擎主体
# ============================================================

class InspectEngine:
    """检测引擎：相机 + Pipeline + 棒号注入 → InspectData。

    生命周期
    --------
    1. ``__init__`` — 仅保存参数，不打开硬件
    2. ``start()`` — 打开相机 + 加载模型（耗时 ~20s，建议在程序启动时调用一次）
    3. ``trigger_once()`` 或 ``run_loop()`` — 跑检测
    4. ``stop()`` — 关闭

    线程安全
    --------
    - ``trigger_once()`` / ``run_loop()`` 不要并发调用（共享相机和 Pipeline 句柄）
    - ``on_inspect`` 回调在调用线程上执行；UI 层负责切回 UI 线程
    """

    def __init__(self,
                 config: Optional[InspectEngineConfig] = None,
                 *,
                 rod_id_provider: Optional[Callable[[], str]] = None,
                 on_inspect: Optional[Callable] = None,
                 on_error: Optional[Callable[[Exception], None]] = None):
        """
        Parameters
        ----------
        config : InspectEngineConfig
            运行配置。``None`` 时使用默认值。
        rod_id_provider : Callable[[], str]
            返回当前棒号的回调。默认返回 ``"NoRead"``（无扫码场景）。
        on_inspect : Callable
            每次检测完成后的回调。在工作线程上调用。

            兼容两种签名（按参数个数自动判别）：

            - ``on_inspect(InspectData)`` — 旧签名，只拿对外契约数据
            - ``on_inspect(InspectData, DetectionResult)`` — 新签名，
              额外拿到 algorithm 层完整结果（含 label_map / crops），
              用于落盘 marked 大图、按类别归档 crop 等场景

        on_error : Callable[[Exception], None]
            异常回调；不抛出，避免 run_loop 静默卡死。
        """
        self.config = config or InspectEngineConfig()
        self.rod_id_provider = rod_id_provider or (lambda: "NoRead")
        self.on_inspect = on_inspect or (lambda d: None)
        self.on_error = on_error or (lambda e: logger.error(f"InspectEngine 错误: {e}", exc_info=True))

        # 一次性探测 on_inspect 接受 1 个还是 2 个参数（避免 trigger_once
        # 每次都跑 inspect.signature — 高频时是无谓开销）
        self._on_inspect_arity = self._detect_arity(self.on_inspect)

        self._camera: Optional[BVCamera] = None
        self._pipeline: Optional[Pipeline] = None
        self._inspect_id_counter = 0
        self._counter_lock = threading.Lock()
        self._started = False

        # 运行循环控制
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_stop = threading.Event()

    @staticmethod
    def _detect_arity(fn: Callable) -> int:
        """探测 callable 的位置参数个数（兼容旧 1 参数签名）。失败 → 1。"""
        import inspect as _inspect
        try:
            sig = _inspect.signature(fn)
            return len([p for p in sig.parameters.values()
                         if p.kind in (p.POSITIONAL_ONLY,
                                        p.POSITIONAL_OR_KEYWORD)])
        except (ValueError, TypeError):
            return 1

    # ─────────── 生命周期 ───────────

    def start(self) -> None:
        """打开相机 + 加载模型。幂等。"""
        if self._started:
            return
        cfg = self.config

        # 1) 打开相机
        try:
            self._camera = BVCamera(uid=cfg.camera_uid)
            self._camera.configure(
                width=cfg.width, height=cfg.height,
                acquisition_mode="SingleFrame",
                trigger_mode="On",
                trigger_source=cfg.trigger_source,
                exposure_us=cfg.exposure_us,
            )
            self._camera.start()
            logger.info(f"相机就绪: {self._camera.model} sn={self._camera.serial}")
        except BVCameraError as e:
            self._cleanup_partial()
            raise RuntimeError(f"相机初始化失败: {e}") from e

        # 2) 加载模型 / 流水线
        try:
            self._pipeline = Pipeline(
                cfg.seg_model, cfg.cls_model, cfg.judge_config,
                ng_trigger_classes=cfg.ng_trigger_classes,
                class_rules=cfg.class_rules,
            )
            logger.info("检测流水线就绪")
        except Exception as e:
            self._cleanup_partial()
            raise RuntimeError(f"流水线加载失败: {e}") from e

        self._started = True
        logger.info("InspectEngine 已启动")

    def stop(self) -> None:
        """停止运行循环 + 关闭相机 + 释放模型。幂等。"""
        # 1) 通知 loop 停止
        self._loop_stop.set()

        # 2) 主动 ImageStop + ImageReqAbortAll 唤醒可能阻塞在 ImageComplete
        #    的 trigger_and_grab（默认 grab_timeout_ms=10000，相机异常时
        #    会一直阻塞到超时）。不做这步则 stop 必然 join 10s 才返回，
        #    且关程序时可能正赶上 close camera 撞 in-flight grab → crash。
        #    BVCamera.stop 自身 idempotent，跨线程调用 BVCAM_ImageReqAbortAll
        #    是 SDK 显式允许的（其设计就是给外部线程取消用）。
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception as e:
                logger.warning(f"中断相机采集流异常（忽略）: {e}")

        # 3) 等 loop 退出
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=10.0)
            if self._loop_thread.is_alive():
                logger.warning("loop 线程超时未退出，强行继续清理")
        self._loop_thread = None

        # 4) 释放硬件
        self._cleanup_partial()
        self._started = False
        logger.info("InspectEngine 已停止")

    # ─────────── 热更新（无需重启） ───────────

    def set_class_rules(self, rules) -> None:
        """运行时热更新 per-class 规则；若 pipeline 已起则同步推进去。"""
        self.config.class_rules = list(rules) if rules else None
        if self._pipeline is not None:
            self._pipeline.set_class_rules(rules or [])

    def set_judge_config(self, judge_config) -> None:
        """运行时热更新全局几何阈值。"""
        self.config.judge_config = judge_config
        if self._pipeline is not None:
            self._pipeline.set_judge_config(judge_config)

    def _cleanup_partial(self) -> None:
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception as e:
                logger.warning(f"关闭相机异常: {e}")
            self._camera = None
        if self._pipeline is not None:
            try:
                self._pipeline.close()
            except Exception as e:
                logger.warning(f"关闭流水线异常: {e}")
            self._pipeline = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    # ─────────── 触发 ───────────

    def trigger_once(self) -> Optional[InspectData]:
        """同步触发一次完整流水线，返回 InspectData。

        失败时返回 ``None``（异常已记录到 ``on_error``，不抛出，
        以便 run_loop 容错继续）。
        """
        if not self._started:
            raise RuntimeError("InspectEngine 未启动，先调用 start()")
        assert self._camera is not None and self._pipeline is not None

        try:
            # 1) 棒号
            rod_id = ""
            try:
                rod_id = self.rod_id_provider() or "NoRead"
            except Exception as e:
                logger.warning(f"棒号获取失败: {e}; 用 NoRead")
                rod_id = "NoRead"

            # 2) 抓图
            frame = self._camera.trigger_and_grab(
                timeout_ms=self.config.grab_timeout_ms,
            )

            # 3) Pipeline — preprocess 由内部按 dtype 自动判别
            #    keep_label_map + keep_crops + keep_raw_input 都开，便于
            #    on_inspect 回调存档全部 4 类图（含 TIF 原图 + WebImage）
            result = self._pipeline.process(
                frame,
                keep_processed_image=True,
                keep_crops=True,
                keep_label_map=True,
                keep_raw_input=True,
            )

            # 4) 装配 InspectData
            with self._counter_lock:
                self._inspect_id_counter += 1
                inspect_id = self._inspect_id_counter
            raw_frame_for_inspect = (
                None if self.config.use_preprocessed_as_inspect_image
                else frame
            )
            data = detection_to_inspect_data(
                result, rod_id=rod_id, inspect_id=inspect_id,
                raw_frame=raw_frame_for_inspect,
            )

            # 5) 回调 — 兼容 1 参数和 2 参数两种签名（arity 在 __init__ 缓存）
            try:
                if self._on_inspect_arity >= 2:
                    self.on_inspect(data, result)
                else:
                    self.on_inspect(data)
            except Exception as cb_e:
                logger.error(f"on_inspect 回调异常: {cb_e}", exc_info=True)

            return data

        except Exception as e:
            self.on_error(e)
            return None

    def stop_loop(self, timeout_s: float = 10.0) -> None:
        """停止后台触发循环但保持引擎打开。"""
        self._loop_stop.set()
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=timeout_s)
        self._loop_thread = None

    def run_loop(self, *, interval_s: float = 2.0,
                 trigger_event: Optional[threading.Event] = None) -> None:
        """启动后台线程周期性触发。

        Parameters
        ----------
        interval_s : float
            两次触发的最小间隔（含本次推理耗时）。
            实际节奏为 ``max(interval_s, 本次耗时)``。
        trigger_event : threading.Event | None
            可选的外部触发信号。若提供，loop 在每次循环开始时等待此事件
            （等价于「外部触发」模式，例如 PLC 信号 / 扫码触发）。
            事件被消费后自动 clear。
        """
        if not self._started:
            raise RuntimeError("InspectEngine 未启动")
        if self._loop_thread and self._loop_thread.is_alive():
            logger.warning("run_loop 已在运行")
            return

        self._loop_stop.clear()

        def _loop():
            logger.info(f"运行循环已启动: interval={interval_s}s, "
                         f"event={'on' if trigger_event else 'off'}")
            while not self._loop_stop.is_set():
                # 外部触发模式
                if trigger_event is not None:
                    if not trigger_event.wait(timeout=1.0):
                        continue
                    trigger_event.clear()

                t0 = time.perf_counter()
                self.trigger_once()
                elapsed = time.perf_counter() - t0

                # 自适应等待：不够的间隔补齐，多了就立即下一轮
                if trigger_event is None:
                    remain = interval_s - elapsed
                    if remain > 0:
                        # 用 stop 事件 wait 以便能立即响应 stop
                        if self._loop_stop.wait(timeout=remain):
                            break
            logger.info("运行循环已退出")

        self._loop_thread = threading.Thread(
            target=_loop, daemon=True, name="InspectEngineLoop",
        )
        self._loop_thread.start()

    # ─────────── 状态查询 ───────────

    @property
    def is_running(self) -> bool:
        return self._started

    @property
    def is_looping(self) -> bool:
        return self._loop_thread is not None and self._loop_thread.is_alive()

    @property
    def inspect_count(self) -> int:
        with self._counter_lock:
            return self._inspect_id_counter
