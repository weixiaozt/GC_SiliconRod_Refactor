"""
日志管理模块
============
提供统一的日志初始化，支持：
  - 控制台输出
  - 文件输出（按天轮转，保留 N 天）
  - 错误日志单独文件
  - 可通过 config.json 配置日志级别、目录、保留天数
"""

import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler


# ─────────────────────────────────────────────
#  常量
# ─────────────────────────────────────────────
_DEFAULT_LOG_DIR = "logs"
_DEFAULT_LEVEL = "INFO"
_DEFAULT_KEEP_DAYS = 30
_LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_initialized = False


# ─────────────────────────────────────────────
#  公开接口
# ─────────────────────────────────────────────
def setup_logging(
    log_dir: str = None,
    level: str = None,
    keep_days: int = None,
    console: bool = True,
):
    """
    初始化全局日志系统。

    Parameters
    ----------
    log_dir : str
        日志文件存放目录，默认为项目根目录下的 ``logs/``。
    level : str
        日志级别，支持 DEBUG / INFO / WARNING / ERROR，默认 INFO。
    keep_days : int
        日志文件保留天数，默认 30。
    console : bool
        是否同时输出到控制台，默认 True。
    """
    global _initialized
    if _initialized:
        return

    log_dir = log_dir or _DEFAULT_LOG_DIR
    level = (level or _DEFAULT_LEVEL).upper()
    keep_days = keep_days if keep_days is not None else _DEFAULT_KEEP_DAYS

    # 确保日志目录存在
    os.makedirs(log_dir, exist_ok=True)

    # 根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level, logging.INFO))

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FMT)

    # ── 1. 全量日志文件（按天轮转）──
    all_log_path = os.path.join(log_dir, "sirod_inspector.log")
    all_handler = TimedRotatingFileHandler(
        all_log_path,
        when="midnight",
        interval=1,
        backupCount=keep_days,
        encoding="utf-8",
    )
    all_handler.suffix = "%Y-%m-%d"
    all_handler.setLevel(getattr(logging, level, logging.INFO))
    all_handler.setFormatter(formatter)
    root_logger.addHandler(all_handler)

    # ── 2. 错误日志文件（仅 WARNING 及以上）──
    err_log_path = os.path.join(log_dir, "sirod_error.log")
    err_handler = TimedRotatingFileHandler(
        err_log_path,
        when="midnight",
        interval=1,
        backupCount=keep_days,
        encoding="utf-8",
    )
    err_handler.suffix = "%Y-%m-%d"
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(formatter)
    root_logger.addHandler(err_handler)

    # ── 3. 控制台输出 ──
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, level, logging.INFO))
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    _initialized = True

    logger = logging.getLogger("SiRod.Logger")
    logger.info(
        f"日志系统已初始化: dir={os.path.abspath(log_dir)}, "
        f"level={level}, keep_days={keep_days}"
    )


def get_logger(name: str) -> logging.Logger:
    """
    获取一个命名 logger。

    推荐命名规范: ``SiRod.模块名``，例如::

        logger = get_logger("SiRod.TCP")
        logger = get_logger("SiRod.Database")
    """
    return logging.getLogger(name)
