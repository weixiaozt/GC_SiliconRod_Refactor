"""
扫码枪 TCP 客户端
==================
对接 Halcon ``Code_Tcp`` 用的协议，等效以下流程::

    open_socket_connect('192.168.12.56', 5000, TCP)
    send_data(s, 'z', 'start', ...)              # Halcon 'z' = zero-terminated string
    while running:
        try:
            receive_data(s, 'z', CHAR, From)     # 接收一个棒号
            cache[rod_id] = CHAR
            wait_seconds(5)
            send_data(s, 'z', 'start', ...)      # 请求下一次扫码
        except: pass

公开 API
--------
    ScannerClient(host, port)
    .start()                          后台线程开始接收
    .stop()
    .current_rod_id() -> str          线程安全读最新棒号
    .latest_at -> datetime | None     最新棒号到达时间
    .is_connected -> bool
"""

from __future__ import annotations

import datetime
import logging
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger("SiRod.Scanner")


# ============================================================
# Halcon 'z' 协议帮助函数
# ============================================================
# Halcon send_data(socket, 'z', value, target)
#   把 value 转成字符串，加 NUL 终止符后发送，UDP 模式才用 target
# Halcon receive_data(socket, 'z', out, from)
#   一直读直到遇到 NUL 终止符，返回不含 NUL 的字符串

_NULL = b"\x00"


def _send_z(sock: socket.socket, text: str) -> None:
    """Halcon 'z' 协议发送：字节流 + NUL 终止"""
    payload = text.encode("ascii", errors="replace") + _NULL
    sock.sendall(payload)


def _recv_z(sock: socket.socket, max_bytes: int = 256) -> Optional[str]:
    """Halcon 'z' 协议接收：读到 NUL 为止。

    返回不含 NUL 的字符串。超时返回 None，连接断开抛 ConnectionError。
    """
    buf = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            # 对端断开
            raise ConnectionError("scanner disconnected")
        if chunk == _NULL:
            return buf.decode("ascii", errors="replace")
        buf.extend(chunk)
        if len(buf) > max_bytes:
            # 协议异常：丢弃直到下一个 NUL
            logger.warning(f"scanner: 收到超长无终止符数据 ({len(buf)}B)，丢弃")
            buf.clear()


# ============================================================
# 扫码枪客户端
# ============================================================

class ScannerClient:
    """扫码枪 TCP 客户端（Halcon Code_Tcp 协议兼容）

    特性：
      - 后台线程循环接收，最新棒号保存供随时查询
      - 自动重连：连接断开后按 ``reconnect_interval_s`` 重试
      - 节流请求：收到一个棒号后等待 ``poll_interval_s`` 再请求下一次
      - 不阻塞业务线程，``current_rod_id()`` 永远立即返回

    与 Halcon Rec_Code 行为一致：
      - 每次循环 ``send "start"`` 发请求
      - 等待对端返回（带 socket 超时，避免永久阻塞）
      - 接收成功后 wait 5 秒（默认）再继续

    使用::

        scanner = ScannerClient("192.168.12.56", 5000)
        scanner.start()
        ...
        rod = scanner.current_rod_id()    # 任何时候、任何线程
        ...
        scanner.stop()
    """

    def __init__(self,
                 host: str = "192.168.12.56", port: int = 5000,
                 *,
                 poll_interval_s: float = 5.0,
                 recv_timeout_s: float = 1.0,
                 reconnect_interval_s: float = 3.0,
                 default_rod_id: str = "NoRead"):
        self.host = host
        self.port = port
        self.poll_interval_s = poll_interval_s
        self.recv_timeout_s = recv_timeout_s
        self.reconnect_interval_s = reconnect_interval_s

        self._default_rod_id = default_rod_id
        self._latest_rod_id = default_rod_id
        self._latest_at: Optional[datetime.datetime] = None
        self._lock = threading.Lock()

        self._sock: Optional[socket.socket] = None
        self._connected = False

        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ─────────── 公开属性 ───────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def latest_at(self) -> Optional[datetime.datetime]:
        with self._lock:
            return self._latest_at

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def current_rod_id(self) -> str:
        """获取最新棒号（线程安全，**不消费** — latest 保持不变）。"""
        with self._lock:
            return self._latest_rod_id

    def take_rod_id(self) -> str:
        """获取最新棒号并 reset 为默认值（消费式，与 Halcon ``Code_Tcp`` 一致）。

        生产场景下推荐用法 — 每次抓图前调一次：
        - 若已扫到新棒号，返回它，本次抓图绑定到这个棒号
        - 若没扫到（队列空），返回 ``NoRead`` 标记本次未扫码
        - 取走后下次调用会再次返回 ``NoRead``，避免老棒号错配给新棒
        """
        with self._lock:
            rod = self._latest_rod_id
            self._latest_rod_id = self._default_rod_id
            self._latest_at = None
            return rod

    # ─────────── 生命周期 ───────────

    def start(self) -> None:
        """启动后台接收线程。幂等。"""
        if self.is_running:
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ScannerClient",
        )
        self._thread.start()
        logger.info(f"扫码枪客户端启动: {self.host}:{self.port}")

    def stop(self, timeout_s: float = 5.0) -> None:
        """停止后台线程并关闭 socket。"""
        self._stop_flag.set()
        # 主动 shutdown socket 以打断阻塞读
        try:
            if self._sock is not None:
                self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=timeout_s)
        self._thread = None
        self._close_sock()
        logger.info("扫码枪客户端已停止")

    # ─────────── 内部 ───────────

    def _close_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._connected = False

    def _connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.recv_timeout_s)
            s.connect((self.host, self.port))
            self._sock = s
            self._connected = True
            logger.info(f"扫码枪已连接: {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.warning(f"扫码枪连接失败 ({self.host}:{self.port}): {e}")
            self._close_sock()
            return False

    def _update_rod_id(self, rod_id: str) -> None:
        rod_id = (rod_id or "").strip()
        if not rod_id:
            return
        with self._lock:
            self._latest_rod_id = rod_id
            self._latest_at = datetime.datetime.now()
        logger.info(f"扫码: {rod_id}")

    def _loop(self) -> None:
        """后台主循环：连接 → 请求 → 接收 → 重试。"""
        while not self._stop_flag.is_set():
            if not self._connected and not self._connect():
                # 重连等待，可被 stop 立即打断
                if self._stop_flag.wait(timeout=self.reconnect_interval_s):
                    break
                continue

            # 发送一次 "start" 请求
            try:
                _send_z(self._sock, "start")
            except Exception as e:
                logger.warning(f"发送扫码请求失败: {e}; 重连")
                self._close_sock()
                continue

            # 等待响应：用短超时循环，便于响应 stop
            start_t = time.monotonic()
            deadline = start_t + 30.0   # 最多等 30s 没收到就重发请求
            got = False
            while not self._stop_flag.is_set() and time.monotonic() < deadline:
                try:
                    rod = _recv_z(self._sock)
                    if rod is not None:
                        self._update_rod_id(rod)
                        got = True
                        break
                except socket.timeout:
                    continue
                except ConnectionError as e:
                    logger.warning(f"扫码枪连接断开: {e}")
                    self._close_sock()
                    break
                except Exception as e:
                    logger.error(f"扫码枪接收异常: {e}", exc_info=True)
                    self._close_sock()
                    break

            if not self._connected:
                # 重连
                continue

            # 收到后 wait poll_interval_s 再请求下一次（节流）
            if got:
                if self._stop_flag.wait(timeout=self.poll_interval_s):
                    break
        logger.info("扫码枪循环已退出")
        self._close_sock()
