"""
班次统计持久化模块
==================
功能：
  1. 将总览页面的统计数据（检测数/合格数/NG数/平均时长）持久化到本地 JSON 文件
  2. 软件重启时自动恢复上次的统计数据
  3. 按配置的班次时间点（默认 8:00 和 20:00）自动清零
  4. 清零时间可通过设置界面修改，保存在 config.json 中

存储文件：项目目录下 shift_stats.json
"""

import json
import os
import logging
import datetime
from typing import Optional

logger = logging.getLogger("SiRod.ShiftStats")

_DEFAULT_STATS = {
    "total": 0,
    "ok_count": 0,
    "ng_count": 0,
    "avg_ms": 0.0,
    "shift_start": "",       # 当前班次开始时间 (ISO 格式)
    "last_updated": "",      # 最后更新时间 (ISO 格式)
}


class ShiftStats:
    """
    班次统计数据管理器。

    职责：
      - 持久化读写统计数据到 shift_stats.json
      - 判断是否需要班次清零
      - 提供清零和更新接口
    """

    def __init__(self, project_dir: str, reset_times: Optional[list] = None):
        """
        参数:
            project_dir: 项目根目录，用于定位 shift_stats.json
            reset_times: 班次清零时间列表，格式 ["08:00", "20:00"]
        """
        self._file_path = os.path.join(project_dir, "shift_stats.json")
        self._reset_times = reset_times or ["08:00", "20:00"]
        self._data = dict(_DEFAULT_STATS)

        # 加载已有数据
        self._load()

        # 检查是否需要班次清零
        if self._should_reset():
            logger.info("检测到跨班次，执行自动清零")
            self.reset()

    # ─────────── 属性 ───────────

    @property
    def total(self) -> int:
        return self._data["total"]

    @property
    def ok_count(self) -> int:
        return self._data["ok_count"]

    @property
    def ng_count(self) -> int:
        return self._data["ng_count"]

    @property
    def avg_ms(self) -> float:
        return self._data["avg_ms"]

    @property
    def shift_start(self) -> str:
        return self._data["shift_start"]

    @property
    def reset_times(self) -> list:
        return list(self._reset_times)

    @reset_times.setter
    def reset_times(self, times: list):
        """更新清零时间配置"""
        self._reset_times = sorted(times)

    # ─────────── 更新统计 ───────────

    def update(self, total: int, ok_count: int, ng_count: int, avg_ms: float):
        """更新统计数据并持久化"""
        self._data["total"] = total
        self._data["ok_count"] = ok_count
        self._data["ng_count"] = ng_count
        self._data["avg_ms"] = avg_ms
        self._data["last_updated"] = datetime.datetime.now().isoformat()
        self._save()

    def reset(self):
        """清零统计数据（班次切换时调用）"""
        now = datetime.datetime.now()
        self._data = dict(_DEFAULT_STATS)
        self._data["shift_start"] = now.isoformat()
        self._data["last_updated"] = now.isoformat()
        self._save()
        logger.info(f"班次统计已清零，新班次开始: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    # ─────────── 班次清零判断 ───────────

    def check_and_reset(self) -> bool:
        """
        检查当前时间是否到达班次清零时间点。

        返回 True 表示已执行清零。
        此方法应由定时器每分钟调用一次。
        """
        if self._should_reset():
            self.reset()
            return True
        return False

    def _should_reset(self) -> bool:
        """
        判断是否需要清零。

        逻辑：
          - 获取上次更新时间和当前时间
          - 检查在上次更新到现在之间，是否经过了任何一个清零时间点
          - 如果经过了，则需要清零
        """
        last_updated_str = self._data.get("last_updated", "")
        if not last_updated_str:
            # 首次运行，不需要清零
            return False

        try:
            last_updated = datetime.datetime.fromisoformat(last_updated_str)
        except (ValueError, TypeError):
            return False

        now = datetime.datetime.now()

        # 如果上次更新时间在未来（时钟异常），不清零
        if last_updated > now:
            return False

        # 检查从上次更新到现在之间是否经过了任何清零时间点
        for time_str in self._reset_times:
            try:
                hour, minute = map(int, time_str.split(":"))
            except (ValueError, AttributeError):
                continue

            # 构造今天的清零时间点
            today_reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # 检查今天的清零时间点是否在 (last_updated, now] 区间内
            if last_updated < today_reset <= now:
                return True

            # 如果上次更新是昨天或更早，还需要检查昨天的清零时间点
            yesterday_reset = today_reset - datetime.timedelta(days=1)
            if last_updated < yesterday_reset <= now:
                return True

        return False

    def get_next_reset_time(self) -> Optional[datetime.datetime]:
        """获取下一个清零时间点"""
        now = datetime.datetime.now()
        candidates = []

        for time_str in self._reset_times:
            try:
                hour, minute = map(int, time_str.split(":"))
            except (ValueError, AttributeError):
                continue

            # 今天的这个时间点
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                # 已过，取明天的
                candidate += datetime.timedelta(days=1)
            candidates.append(candidate)

        if candidates:
            return min(candidates)
        return None

    # ─────────── 文件读写 ───────────

    def _load(self):
        """从 JSON 文件加载统计数据"""
        if not os.path.isfile(self._file_path):
            logger.info("统计文件不存在，使用默认值")
            # 设置初始班次开始时间
            self._data["shift_start"] = datetime.datetime.now().isoformat()
            self._data["last_updated"] = datetime.datetime.now().isoformat()
            self._save()
            return

        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            # 合并数据（兼容旧版本缺少字段的情况）
            for key in _DEFAULT_STATS:
                if key in loaded:
                    self._data[key] = loaded[key]

            logger.info(
                f"统计数据已恢复: 检测={self._data['total']}, "
                f"合格={self._data['ok_count']}, NG={self._data['ng_count']}"
            )
        except Exception as e:
            logger.warning(f"加载统计文件失败: {e}")

    def _save(self):
        """将统计数据保存到 JSON 文件（原子写 — 同 config.save 的修复理由）"""
        try:
            os.makedirs(os.path.dirname(self._file_path) or ".", exist_ok=True)
            tmp = self._file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=4, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self._file_path)
        except Exception as e:
            logger.error(f"保存统计文件失败: {e}")
            try:
                if os.path.exists(self._file_path + ".tmp"):
                    os.remove(self._file_path + ".tmp")
            except OSError:
                pass

    def as_dict(self) -> dict:
        """返回当前统计数据的副本"""
        return dict(self._data)
