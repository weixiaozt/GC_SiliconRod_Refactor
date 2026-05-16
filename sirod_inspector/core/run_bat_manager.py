"""
Run.bat 子进程管理器
==================
负责在主程序启动时拉起根目录下的 Run.bat，
监控其运行状态，并在主程序关闭时一并终止整个进程树。

核心要点：
  • Windows 下 bat 文件常会 spawn 子进程（cmd → 实际程序），
    必须用 CREATE_NEW_PROCESS_GROUP 才能用 taskkill /T 整树终止；
  • 单独用 process.terminate() 只杀 cmd.exe，bat 启动的实际程序会变成孤儿；
  • 用 QTimer + poll() + psutil 双重检查存活状态，避免 cmd 退了但子进程还活着；
  • atexit 保险：Python 异常退出时也尽力清理。

公开 API:
    RunBatManager(bat_path, on_status_changed=callback)
    .start()       → bool, 启动 bat
    .stop()        → bool, 终止整个进程树
    .is_running    → bool, 当前是否运行
    .pid           → int|None
"""
import atexit
import os
import subprocess
import sys
import threading
import time

from core.logger import get_logger

logger = get_logger("SiRod.RunBat")

# psutil 用于深度检查子进程树（可选依赖）
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    logger.warning("psutil 未安装，进程树检查将简化（建议 pip install psutil）")


class RunBatManager:
    """Run.bat 子进程的生命周期管理"""

    def __init__(self, bat_path: str, on_status_changed=None):
        """
        :param bat_path: Run.bat 的绝对路径
        :param on_status_changed: 状态变化回调 callable(running: bool)，
                                  用于主线程更新 UI（注意线程安全）
        """
        self.bat_path = bat_path
        self._process = None
        self._on_status_changed = on_status_changed
        self._lock = threading.Lock()
        self._last_known_running = False
        self._stop_requested = False  # 标记是否是用户主动停止（防止误报"意外退出"）

        # 注册 atexit 兜底，防止主程序异常退出时 bat 残留
        atexit.register(self._atexit_cleanup)

    # ─────────── 状态属性 ───────────
    @property
    def pid(self):
        return self._process.pid if self._process else None

    @property
    def is_running(self) -> bool:
        """当前 bat 进程是否仍在运行"""
        if self._process is None:
            return False
        # poll() 返回 None 表示子进程仍在运行
        if self._process.poll() is not None:
            return False
        return True

    def is_tree_alive(self) -> bool:
        """更深度的检查：bat 启动的整个进程树是否还有进程存活。

        例如 Run.bat 里 `start xxx.exe` 后 cmd 可能已退出，
        但 xxx.exe 还在运行 — 此时也算"运行中"。
        """
        if self._process is None:
            return False

        # 主进程仍在
        if self._process.poll() is None:
            return True

        # 主进程退了，但子进程可能还在
        if HAS_PSUTIL and self._process.pid:
            try:
                # 如果原 pid 已不在系统中，说明真的全没了
                parent = psutil.Process(self._process.pid)
                children = parent.children(recursive=True)
                return any(c.is_running() for c in children)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False

        return False

    # ─────────── 启动 ───────────
    def start(self) -> bool:
        """启动 Run.bat。失败返回 False（不抛异常）"""
        if self.is_running:
            logger.warning("Run.bat 已在运行，重复启动被忽略")
            return True

        if not os.path.isfile(self.bat_path):
            logger.error(f"Run.bat 不存在: {self.bat_path}")
            self._notify(False)
            return False

        if not sys.platform.startswith("win"):
            logger.warning(f"非 Windows 平台 ({sys.platform})，Run.bat 启动跳过")
            self._notify(False)
            return False

        try:
            # CREATE_NEW_PROCESS_GROUP: 让 bat 拥有独立进程组，便于后续整树 taskkill
            # CREATE_NEW_CONSOLE: 让 bat 在新的控制台窗口运行，不污染主程序日志
            creationflags = 0
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "CREATE_NEW_CONSOLE"):
                creationflags |= subprocess.CREATE_NEW_CONSOLE

            # 工作目录设为 bat 所在目录，因为 bat 内部可能有相对路径
            work_dir = os.path.dirname(os.path.abspath(self.bat_path))

            self._stop_requested = False
            self._process = subprocess.Popen(
                ["cmd.exe", "/c", self.bat_path],
                cwd=work_dir,
                creationflags=creationflags,
                # 不接管 stdout/stderr — bat 会有自己的控制台窗口
            )

            logger.info(
                f"Run.bat 已启动: pid={self._process.pid}, "
                f"path={self.bat_path}, cwd={work_dir}"
            )
            self._last_known_running = True
            self._notify(True)
            return True

        except Exception as e:
            logger.error(f"启动 Run.bat 失败: {e}", exc_info=True)
            self._process = None
            self._notify(False)
            return False

    # ─────────── 停止 ───────────
    def stop(self, timeout: float = 5.0) -> bool:
        """终止 Run.bat 及其整个进程树"""
        with self._lock:
            self._stop_requested = True

            if self._process is None:
                logger.info("Run.bat 未启动，无需停止")
                return True

            pid = self._process.pid

            # 第一步：psutil 主动杀整棵树（最可靠）
            if HAS_PSUTIL:
                try:
                    parent = psutil.Process(pid)
                    children = parent.children(recursive=True)
                    # 先杀子进程，再杀父进程
                    for child in children:
                        try:
                            child.terminate()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    try:
                        parent.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                    # 等一下，给进程时间优雅退出
                    psutil.wait_procs([parent] + children, timeout=timeout / 2)

                    # 仍存活的强杀
                    for proc in [parent] + children:
                        try:
                            if proc.is_running():
                                proc.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                    logger.info(f"Run.bat 进程树已通过 psutil 终止: pid={pid}")

                except psutil.NoSuchProcess:
                    logger.info(f"Run.bat 进程 {pid} 已不存在")
                except Exception as e:
                    logger.warning(f"psutil 终止失败，回退到 taskkill: {e}")
                    self._taskkill_fallback(pid)
            else:
                # 第二步（回退）：用 Windows 的 taskkill /T 整树终止
                self._taskkill_fallback(pid)

            # 兜底：subprocess.Popen 自己也尝试一下
            try:
                if self._process.poll() is None:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
            except Exception:
                pass

            self._process = None
            self._last_known_running = False
            self._notify(False)
            return True

    @staticmethod
    def _taskkill_fallback(pid: int):
        """Windows taskkill 整树终止（兜底方案）"""
        if not sys.platform.startswith("win"):
            return
        try:
            # /F 强制 /T 树状（含子进程） /PID 指定 pid
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            logger.info(
                f"taskkill 结果 (pid={pid}): "
                f"rc={result.returncode}, stdout={result.stdout.strip()}"
            )
        except Exception as e:
            logger.error(f"taskkill 执行失败: {e}")

    # ─────────── 状态轮询（供外部 QTimer 调用）───────────
    def poll_status(self) -> bool:
        """供 UI 定时器调用，检查状态变化并触发回调。返回当前是否运行。"""
        running = self.is_tree_alive()

        # 状态变化时记日志 + 回调
        if running != self._last_known_running:
            if running:
                logger.info(f"Run.bat 状态变化: 启动 (pid={self.pid})")
            else:
                if self._stop_requested:
                    logger.info("Run.bat 状态变化: 已停止（用户触发）")
                else:
                    logger.warning(
                        f"Run.bat 状态变化: 意外退出（pid={self.pid}）"
                    )
            self._last_known_running = running
            self._notify(running)

        return running

    # ─────────── 内部辅助 ───────────
    def _notify(self, running: bool):
        """触发外部回调，捕获回调异常防止打断管理逻辑"""
        if self._on_status_changed is None:
            return
        try:
            self._on_status_changed(bool(running))
        except Exception as e:
            logger.error(f"Run.bat 状态回调异常: {e}", exc_info=True)

    def _atexit_cleanup(self):
        """atexit 兜底清理 — Python 解释器退出时调用"""
        if self._process is not None and self._process.poll() is None:
            logger.info("atexit 兜底：清理仍在运行的 Run.bat")
            try:
                self.stop(timeout=2.0)
            except Exception as e:
                logger.error(f"atexit 清理失败: {e}")
