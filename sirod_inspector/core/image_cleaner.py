"""
存图自动清理
============
检测产生的图按"保留天数"自动删，防硬盘越攒越满。

为什么要差异化保留（不同图体积/价值差很多）::

    ImageRaw/<stem>.tif          uint16 原图 ~30MB/根，空间大头，价值低 → 短保留
    images/<date>/full/*/OK/     OK 全图，量大价值低           → 短保留
    images/<date>/full/*/NG/     NG 全图，少且重要（复检/留档） → 长保留
    images/<date>/crops/         缺陷小图，多是 NG 相关         → 长保留
    WebImage/<stem>.png          NG 给 MES 拉图                 → 长保留

删除依据
--------
- **按文件 mtime**（修改时间）判断，不靠文件名 —— 因为 ``ImageRaw``/``WebImage``
  是平铺的，``stem = <棒号>_<HHMMSS_微秒>`` 不含年月日，只能看 mtime。
- ``images/<date>/`` 虽有日期目录，这里也统一用 mtime（简单一致，且能精确到
  每个 OK/NG 子目录用不同天数）。

安全约束（删文件是破坏性操作，务必谨慎）
----------------------------------------
1. **路径白名单**：只在传入的 3 个目录（base_dir/raw_tif_dir/web_image_dir）
   下操作，绝不碰别处。
2. **不删今天**：cutoff 至少往前推到「今天 0 点」之前，正在写的当天图绝不删。
3. **dry_run**：只 log「将删 X 个 / Y GB」，不真删 —— 现场先开 dry_run 观察几天
   确认没误删，再关掉真删。
4. **删失败不崩**：单个文件删失败 log warning 继续，不影响主程序。
5. **retain_days <= 0 视为「不删该类」**（保护：防手滑配 0 把当天全删了）。

用法::

    cleaner = ImageCleaner(
        base_dir="D:/SiRod_v2/images",
        raw_tif_dir="D:/SiRod_v2/ImageRaw",
        web_image_dir="D:/SiRod_v2/WebImage",
        cleanup_cfg={"enabled": True, "dry_run": False,
                     "retain_days": {"tif": 7, "ok_full": 7,
                                     "ng_full": 30, "crops": 30, "webimage": 30}},
    )
    stats = cleaner.cleanup()   # {"deleted": N, "freed_bytes": B, "by_class": {...}}
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("SiRod.ImageCleaner")

_SECONDS_PER_DAY = 86400.0


@dataclass
class _RetainDays:
    """各类图的保留天数。<=0 = 不删该类。"""
    tif: float = 7.0          # ImageRaw/*.tif
    ok_full: float = 7.0      # images/<date>/full/{raw,marked}/OK/
    ng_full: float = 30.0     # images/<date>/full/{raw,marked}/NG/
    crops: float = 30.0       # images/<date>/crops/
    webimage: float = 30.0    # WebImage/*.png


@dataclass
class CleanupStats:
    deleted: int = 0
    freed_bytes: int = 0
    errors: int = 0
    by_class: dict = field(default_factory=dict)   # {"tif": (n, bytes), ...}

    def add(self, cls: str, n: int, b: int) -> None:
        on, ob = self.by_class.get(cls, (0, 0))
        self.by_class[cls] = (on + n, ob + b)
        self.deleted += n
        self.freed_bytes += b

    @property
    def freed_mb(self) -> float:
        return self.freed_bytes / 1024.0 / 1024.0


class ImageCleaner:
    """按保留天数清理存图目录。线程安全：无共享可变状态，可在后台线程跑。"""

    def __init__(self, *,
                 base_dir: str,
                 raw_tif_dir: str,
                 web_image_dir: str,
                 cleanup_cfg: Optional[dict] = None):
        cfg = cleanup_cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.dry_run = bool(cfg.get("dry_run", True))   # 默认 dry_run 安全

        rd = cfg.get("retain_days", {}) or {}
        self.retain = _RetainDays(
            tif=float(rd.get("tif", 7)),
            ok_full=float(rd.get("ok_full", 7)),
            ng_full=float(rd.get("ng_full", 30)),
            crops=float(rd.get("crops", 30)),
            webimage=float(rd.get("webimage", 30)),
        )

        # 规整路径（绝对 + 规范分隔符）。调用方应传入已 resolve 的绝对路径。
        self.base_dir = os.path.abspath(base_dir) if base_dir else ""
        self.raw_tif_dir = os.path.abspath(raw_tif_dir) if raw_tif_dir else ""
        self.web_image_dir = os.path.abspath(web_image_dir) if web_image_dir else ""

    # ─────────── 公开入口 ───────────

    def cleanup(self) -> CleanupStats:
        """执行一次清理。返回统计。失败不抛异常。"""
        stats = CleanupStats()
        if not self.enabled:
            logger.debug("存图清理未启用 (cleanup.enabled=false)")
            return stats

        # 今天 0 点的时间戳 — cutoff 绝不晚于此，保护当天正在写的图
        now = time.time()
        today_start = now - (now % _SECONDS_PER_DAY)
        tag = "[DRY-RUN 不真删] " if self.dry_run else ""

        logger.info(
            f"{tag}存图清理开始: retain(天) tif={self.retain.tif} "
            f"ok_full={self.retain.ok_full} ng_full={self.retain.ng_full} "
            f"crops={self.retain.crops} web={self.retain.webimage}"
        )

        # 1) TIF 原图（平铺，按 mtime）
        self._clean_flat_dir(self.raw_tif_dir, self.retain.tif,
                             today_start, "tif", stats, ext=".tif")
        # 2) WebImage（平铺，按 mtime）
        self._clean_flat_dir(self.web_image_dir, self.retain.webimage,
                             today_start, "webimage", stats, ext=".png")
        # 3) images/<date>/ 树（OK/NG/crops 各自天数）
        self._clean_images_tree(today_start, stats)

        # 删完顺手清理空目录
        if not self.dry_run:
            self._prune_empty_dirs(self.base_dir)

        logger.info(
            f"{tag}存图清理完成: 共删 {stats.deleted} 个文件, "
            f"释放 {stats.freed_mb:.0f} MB, 失败 {stats.errors}; "
            f"明细 {self._fmt_by_class(stats)}"
        )
        return stats

    # ─────────── 内部：平铺目录（ImageRaw / WebImage）───────────

    def _clean_flat_dir(self, directory: str, retain_days: float,
                        today_start: float, cls: str,
                        stats: CleanupStats, *, ext: str) -> None:
        if not directory or retain_days <= 0:
            return
        if not os.path.isdir(directory):
            return
        # cutoff = min(now - retain_days, 今天0点)；保证今天的绝不删
        cutoff = min(time.time() - retain_days * _SECONDS_PER_DAY, today_start)

        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    if ext and not entry.name.lower().endswith(ext):
                        continue
                    try:
                        st = entry.stat()
                        if st.st_mtime < cutoff:
                            self._delete(entry.path, st.st_size, cls, stats)
                    except OSError as e:
                        logger.warning(f"读取文件状态失败 {entry.path}: {e}")
                        stats.errors += 1
        except OSError as e:
            logger.warning(f"扫描目录失败 {directory}: {e}")

    # ─────────── 内部：images/<date>/ 树 ───────────

    def _clean_images_tree(self, today_start: float, stats: CleanupStats) -> None:
        """遍历 images/<date>/，对 full/*/OK、full/*/NG、crops 用各自天数。"""
        if not self.base_dir or not os.path.isdir(self.base_dir):
            return

        # 每类目标子路径 → (retain_days, cls 名)。相对 <date> 目录。
        # full/raw/OK、full/marked/OK → ok_full
        # full/raw/NG、full/marked/NG → ng_full
        # crops/raw、crops/marked     → crops
        targets = [
            (os.path.join("full", "raw", "OK"),     self.retain.ok_full, "ok_full"),
            (os.path.join("full", "marked", "OK"),  self.retain.ok_full, "ok_full"),
            (os.path.join("full", "raw", "NG"),     self.retain.ng_full, "ng_full"),
            (os.path.join("full", "marked", "NG"),  self.retain.ng_full, "ng_full"),
            (os.path.join("crops", "raw"),          self.retain.crops,   "crops"),
            (os.path.join("crops", "marked"),       self.retain.crops,   "crops"),
        ]

        try:
            date_dirs = [e for e in os.scandir(self.base_dir) if e.is_dir()]
        except OSError as e:
            logger.warning(f"扫描存图根失败 {self.base_dir}: {e}")
            return

        for date_entry in date_dirs:
            for rel, days, cls in targets:
                if days <= 0:
                    continue
                sub = os.path.join(date_entry.path, rel)
                if not os.path.isdir(sub):
                    continue
                cutoff = min(time.time() - days * _SECONDS_PER_DAY, today_start)
                self._clean_dir_recursive(sub, cutoff, cls, stats)

    def _clean_dir_recursive(self, directory: str, cutoff: float,
                            cls: str, stats: CleanupStats) -> None:
        """递归删 directory 下 mtime < cutoff 的文件。"""
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_dir():
                            self._clean_dir_recursive(entry.path, cutoff, cls, stats)
                        elif entry.is_file():
                            st = entry.stat()
                            if st.st_mtime < cutoff:
                                self._delete(entry.path, st.st_size, cls, stats)
                    except OSError as e:
                        logger.warning(f"处理失败 {entry.path}: {e}")
                        stats.errors += 1
        except OSError as e:
            logger.warning(f"扫描目录失败 {directory}: {e}")

    # ─────────── 内部：删除 + 清空目录 ───────────

    def _delete(self, path: str, size: int, cls: str, stats: CleanupStats) -> None:
        # 白名单二次校验：路径必须在三个目录之一下（防御编程）
        if not self._is_under_managed_dir(path):
            logger.warning(f"路径不在受管目录下，跳过（安全保护）: {path}")
            return
        if self.dry_run:
            logger.debug(f"[DRY-RUN] 将删 {path} ({size/1024:.0f} KB)")
            stats.add(cls, 1, size)
            return
        try:
            os.remove(path)
            stats.add(cls, 1, size)
        except OSError as e:
            logger.warning(f"删除失败 {path}: {e}")
            stats.errors += 1

    def _is_under_managed_dir(self, path: str) -> bool:
        ap = os.path.abspath(path)
        for d in (self.base_dir, self.raw_tif_dir, self.web_image_dir):
            if not d:
                continue
            try:
                if os.path.commonpath([ap, d]) == d:
                    return True
            except ValueError:
                continue   # 不同盘符 commonpath 抛 ValueError
        return False

    def _prune_empty_dirs(self, root: str) -> None:
        """删空目录（删完文件后 <date>/full/raw/OK 等可能空了）。不删 root 自己。"""
        if not root or not os.path.isdir(root):
            return
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if os.path.abspath(dirpath) == os.path.abspath(root):
                continue
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass

    @staticmethod
    def _fmt_by_class(stats: CleanupStats) -> str:
        if not stats.by_class:
            return "(无)"
        return ", ".join(
            f"{k}={n}个/{b/1024/1024:.0f}MB"
            for k, (n, b) in sorted(stats.by_class.items())
        )
