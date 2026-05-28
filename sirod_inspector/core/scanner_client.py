"""
扫码枪 TCP 客户端（iter53 = 海康 MV-ID 系列 TCP 触发协议）
==========================================================
**iter45-52 集体翻车原因 / iter53 真相揭晓**

现场扫码枪 = **海康威视 MV-ID2023XM-08M-RBN**。该设备的 TCP 协议跟
``<Set/Exec,cmd>`` 完全无关，配置截图清清楚楚（设置 → I/O 输入）::

    触发模式 = 开启
    触发源   = TCP服务端
    TCP触发端口        = 5000
    TCP触发文本        = "start"        ← 必须发字面 "start" 字符串才触发扫码
    TCP服务器返回数据到触发端口 = ON   ← 扫到的码从同一端口 5000 返回
    命令持续触发 = ON

也就是说协议简单到吓人：

1. 连 192.168.12.56:5000
2. 发 ``b"start"``（裸字符串，匹配设备配的 "TCP 触发文本"）
3. 在 5000 上 recv 收 barcode（裸字符串，可能带 \\r\\n / \\x00 / 无终止符）

老 Halcon ``send_data 'z' 'start'`` 发的是 ``b"start\\x00"``。海康按字面前缀
匹配 "start" → 触发 → push barcode。所以 Halcon 一直能 work。

我们之前 iter45/50/51 发 ``<Set,TriMode,1>``、``<Exec,TriSoft>`` 等命令——
海康根本不认这些字符串，所以**没触发任何一次扫码**，自然 0 barcode。
iter47/48/49 发了 ``b"start\\x00"`` 思路对，但 NUL 终止符 parser 把推回的
裸字符串 barcode 卡在 buffer 里识别不出来。

iter53 = 简单粗暴回归本质：
  - 不发任何 ``<Set,...>`` / ``<Exec,...>``
  - 周期发 ``b"start"``（对齐 Halcon Rec_Code:7354 wait 5s + send start 节奏）
  - recv 收到的字节按多种终止符（``\\x00`` / ``\\r\\n`` / ``\\n`` / ``\\r``）拆
  - 过滤掉触发文本 "start" 自身的回显（如果有的话）
  - barcode 同时兼容 ``<Data,...>`` 和裸字符串两种格式
"""

from __future__ import annotations

import datetime
import logging
import re
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger("SiRod.Scanner")


_TRIGGER_TEXT = b"start\x00"   # 海康"TCP 触发文本"=start，+ NUL 跟老 Halcon send_data 'z' 实际发的 6 字节一致
_TERMINATORS = (b"\x00", b"\r\n", b"\n", b"\r")

# ★看门狗★ 连续这么多根棒「检测完成却没扫到码」就判半开/僵尸连接、强制重连。
# 想改阈值直接改这里（按需求不开放到 config.json）。
_ZOMBIE_MISS_LIMIT = 5


def _apply_socket_tuning(sock: socket.socket) -> None:
    """禁 Nagle + 开 KEEPALIVE，工业 socket 标配"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as e:
        logger.warning(f"TCP_NODELAY 设置失败（不致命）: {e}")
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError as e:
        logger.warning(f"SO_KEEPALIVE 设置失败（不致命）: {e}")


def _split_one_message(buf: bytearray) -> Optional[bytes]:
    """从 buf 头部找一条完整 message，返回 (msg_bytes, 已消费长度) 形式。

    支持的 message 形式（按出现顺序检测）::
        <...>                        — 尖括号包裹的命令/数据
        XXX\\x00                      — NUL 终止
        XXX\\r\\n / XXX\\n / XXX\\r   — 行终止
    返回 None 表示 buf 里没有完整 message（等更多字节）。
    """
    if not buf:
        return None

    # 形式 1：<...>
    if buf[0:1] == b"<":
        end = buf.find(b">")
        if end == -1:
            return None
        msg = bytes(buf[:end + 1])
        del buf[:end + 1]
        return msg

    # 形式 2/3：找最近的终止符
    earliest = -1
    earliest_term_len = 0
    for term in _TERMINATORS:
        idx = buf.find(term)
        if idx != -1 and (earliest == -1 or idx < earliest):
            earliest = idx
            earliest_term_len = len(term)
    if earliest == -1:
        # 没终止符，等更多字节
        # 但如果 buf 已经很大（>256B 还没终止符）就当作一条 message 强吐
        if len(buf) > 256:
            msg = bytes(buf)
            buf.clear()
            return msg
        return None

    msg = bytes(buf[:earliest])
    del buf[:earliest + earliest_term_len]
    return msg


_BARCODE_RE = re.compile(r"^[A-Za-z0-9\-_/. ]+$")


def _extract_barcode(msg: bytes) -> Optional[str]:
    """把一条 message 提取成 barcode 字符串。返回 None = 这不是 barcode。

    兼容：
        <Data,XJN...>        → "XJN..."
        <Read,XJN...>        → "XJN..."
        <Code,XJN...>        → "XJN..."
        <NoRead>             → None（明确没扫到）
        裸字符串 "XJN..."    → "XJN..."
        "start" 触发回显     → None（过滤掉）
        其他非字母数字串     → None
    """
    s = msg.decode("ascii", errors="replace").strip()
    if not s:
        return None

    # 形式 1：<Type,Value>
    if s.startswith("<") and s.endswith(">"):
        inner = s[1:-1]
        parts = inner.split(",", 1)
        type_ = parts[0]
        val = parts[1] if len(parts) > 1 else ""
        if type_ in ("Data", "Read", "Code") and val:
            return val.strip()
        if type_ in ("NoRead", "noread"):
            return None
        return None

    # 形式 2：裸字符串
    # 过滤触发文本自身回显（"start"）
    if s.lower() == "start":
        return None
    # 过滤 NoRead 字面值
    if s.lower() in ("noread", "no_read", "no read"):
        return None
    # 看着像 barcode 就当 barcode（字母数字 + 简单符号）
    if _BARCODE_RE.match(s):
        return s
    return None


# ============================================================
# 扫码枪客户端
# ============================================================

class ScannerClient:
    """扫码枪 TCP 客户端 (iter53, 海康 MV-ID TCP 触发协议)

    协议::
        send b"start"                    # 触发扫码（对应海康"TCP 触发文本"配置）
        recv → barcode bytes (裸字符串或 <Data,..>，可能带 \\x00/\\r\\n)

    复用老 Halcon 节奏：连接时发一次 start + 每个 poll_interval_s 重发一次。
    完全不发 ``<Set,...>`` 任何配置命令，保留扫码枪当前 firmware 配置不动。
    """

    def __init__(self,
                 host: str = "192.168.12.56", port: int = 5000,
                 *,
                 poll_interval_s: float = 5.0,
                 recv_timeout_s: float = 1.0,
                 reconnect_interval_s: float = 3.0,
                 heartbeat_interval_s: float = 30.0,
                 default_rod_id: str = "NoRead"):
        self.host = host
        self.port = port
        self.poll_interval_s = poll_interval_s
        self.recv_timeout_s = recv_timeout_s
        self.reconnect_interval_s = reconnect_interval_s
        self.heartbeat_interval_s = heartbeat_interval_s

        self._default_rod_id = default_rod_id
        self._latest_rod_id = default_rod_id
        self._latest_at: Optional[datetime.datetime] = None
        self._lock = threading.Lock()

        self._sock: Optional[socket.socket] = None
        self._connected = False

        # 诊断统计
        self._recv_bytes_total = 0
        self._barcodes_total = 0
        self._triggers_total = 0
        self._recv_bytes_window = 0
        self._barcodes_window = 0
        self._triggers_window = 0
        self._messages_window = 0
        self._last_heartbeat_t = 0.0
        self._watchdog_reconnects_total = 0   # ★看门狗★ 累计强制重连次数（诊断用）
        # ★看门狗★ 自上次扫到码以来"检测完成(确有棒经过)"的连续根数。
        # notify_activity() 每完成一根 +1；扫到码清 0；连续 _ZOMBIE_MISS_LIMIT 根没码 → 重连。
        self._rods_since_barcode = 0

        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ─────────── 公开 API ───────────

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
        with self._lock:
            return self._latest_rod_id

    def take_rod_id(self) -> str:
        with self._lock:
            rod = self._latest_rod_id
            self._latest_rod_id = self._default_rod_id
            self._latest_at = None
            return rod

    def take_if(self, expected: str) -> bool:
        with self._lock:
            if self._latest_rod_id == expected:
                self._latest_rod_id = self._default_rod_id
                self._latest_at = None
                return True
            return False

    def notify_activity(self) -> None:
        """由检测流水线在「每完成一次检测(确有棒经过)」时调用 = 一根棒过去了。

        看门狗逻辑（只此一条）：有棒过、却连续 `_ZOMBIE_MISS_LIMIT` 根没扫到码 → 判半开/
        僵尸连接、强制重连。这里每完成一根 +1；一旦扫到码就清 0（见 `_update_rod_id`）。
        纯空闲（没棒过）时这里根本不会被调用 → 计数不涨 → 永不重连。
        """
        with self._lock:
            self._rods_since_barcode += 1

    def stats_snapshot(self) -> dict:
        return {
            "recv_bytes_total": self._recv_bytes_total,
            "barcodes_total": self._barcodes_total,
            "triggers_total": self._triggers_total,
            "watchdog_reconnects_total": self._watchdog_reconnects_total,
            "rods_since_barcode": self._rods_since_barcode,
            "connected": self._connected,
        }

    # ─────────── 生命周期 ───────────

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ScannerClient",
        )
        self._thread.start()
        logger.info(
            f"扫码枪客户端启动 (iter54 海康 TCP 触发协议 + stale-flush parser): "
            f"{self.host}:{self.port} poll={self.poll_interval_s}s "
            f"recv_timeout={self.recv_timeout_s}s"
        )

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_flag.set()
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
            _apply_socket_tuning(s)
            s.settimeout(2.0)
            s.connect((self.host, self.port))
            s.settimeout(self.recv_timeout_s)
            self._sock = s
            self._connected = True
            logger.info(
                f"扫码枪已连接: {self.host}:{self.port} "
                f"(NODELAY+KEEPALIVE on, recv_timeout={self.recv_timeout_s}s)"
            )
            return True
        except Exception as e:
            logger.warning(f"扫码枪连接失败 ({self.host}:{self.port}): {e}")
            self._close_sock()
            return False

    def _send_trigger(self) -> bool:
        """发触发文本 b"start"（跟海康配置一致）"""
        if not self._connected or self._sock is None:
            return False
        try:
            self._sock.sendall(_TRIGGER_TEXT)
            self._triggers_total += 1
            self._triggers_window += 1
            logger.debug(
                f"→ {_TRIGGER_TEXT!r} (total triggers={self._triggers_total})"
            )
            return True
        except Exception as e:
            logger.warning(f"发送触发文本失败: {e}; 关连接重连")
            self._close_sock()
            return False

    def _update_rod_id(self, rod_id: str) -> None:
        rod_id = (rod_id or "").strip()
        if not rod_id:
            return
        with self._lock:
            self._latest_rod_id = rod_id
            self._latest_at = datetime.datetime.now()
            self._rods_since_barcode = 0   # ★看门狗★ 扫到码 → 连续没码计数清零
        self._barcodes_total += 1
        self._barcodes_window += 1
        logger.info(f"扫码: {rod_id}  (total={self._barcodes_total})")

    def _maybe_heartbeat(self) -> None:
        now = time.monotonic()
        if self._last_heartbeat_t == 0.0:
            self._last_heartbeat_t = now
            return
        if (now - self._last_heartbeat_t) < self.heartbeat_interval_s:
            return
        logger.info(
            f"[heartbeat] connected={self._connected} "
            f"last {self.heartbeat_interval_s:.0f}s: "
            f"recv_bytes={self._recv_bytes_window} barcodes={self._barcodes_window} "
            f"triggers={self._triggers_window} msgs={self._messages_window} "
            f"| total: recv_bytes={self._recv_bytes_total} "
            f"barcodes={self._barcodes_total} triggers={self._triggers_total} "
            f"wd_reconnects={self._watchdog_reconnects_total} miss={self._rods_since_barcode}"
        )
        self._recv_bytes_window = 0
        self._barcodes_window = 0
        self._triggers_window = 0
        self._messages_window = 0
        self._last_heartbeat_t = now

    def _drain_and_dispatch(self, buf: bytearray) -> None:
        """从 buf 头部尽可能拆 message 并派发。"""
        while True:
            msg = _split_one_message(buf)
            if msg is None:
                return
            self._messages_window += 1
            barcode = _extract_barcode(msg)
            if barcode:
                self._update_rod_id(barcode)
            else:
                # 不是 barcode 但收到了 → log 一下方便诊断
                logger.info(
                    f"扫码响应（非 barcode，可能是 ACK / NoRead）: {msg!r}"
                )

    def _flush_stale_buffer(self, buf: bytearray) -> None:
        """★ iter54 核心修复 ★ 把 buffer 里没终止符的剩余字节当一条 message 冲刷。

        触发条件：buf 非空 + 距上次 recv 已 idle ≥ stale_flush_s（默认 1s）
        对齐 Halcon ``receive_data 'z'`` timeout fallback 行为：超时即返回已收数据。
        """
        if not buf:
            return
        msg = bytes(buf)
        buf.clear()
        self._messages_window += 1
        barcode = _extract_barcode(msg)
        if barcode:
            self._update_rod_id(barcode)
        else:
            logger.info(
                f"扫码响应（无终止符 stale-flush）: {msg!r}"
            )

    def _loop(self) -> None:
        """后台主循环 (iter54)。

        节奏（仿 Halcon Rec_Code）::
            连接 + 发一次 'start'
            loop:
                recv 1s timeout
                if 有新字节: drain buffer 按终止符切 message
                if recv 超时 + buf 非空 + 静默 ≥1s: 把剩余字节冲刷成 message
                if (距上次 trigger > poll_interval_s): 再发一次 'start' 兜底
                心跳

        iter53→iter54 关键变化：海康会推无终止符的裸 barcode 字符串
        （如 ``b"XJN2604BB0931W0-4"`` 19 字节）。iter53 严格等终止符会卡死。
        iter54 加入 stale-flush：1s 静默就把 buffer 当 message 冲刷出来。
        """
        self._last_heartbeat_t = time.monotonic()
        buf = bytearray()
        last_trigger_t = 0.0
        last_recv_t = 0.0   # buffer 上次收到字节的时刻（stale-flush 用）
        stale_flush_s = 1.0

        while not self._stop_flag.is_set():
            if not self._connected:
                if not self._connect():
                    if self._stop_flag.wait(timeout=self.reconnect_interval_s):
                        break
                    continue
                # 连接成功 → 立即触发一次
                buf.clear()
                last_recv_t = 0.0
                if not self._send_trigger():
                    continue
                last_trigger_t = time.monotonic()
                logger.info("★ iter54 ★ init 触发 'start' 已发送（含 stale-flush parser）")

            # 1) recv（短 timeout，让循环能及时跑 heartbeat / trigger keepalive）
            got_data = False
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    logger.warning("扫码枪 recv 返回空 → 对端 FIN，重连")
                    self._close_sock()
                    continue
                self._recv_bytes_total += len(chunk)
                self._recv_bytes_window += len(chunk)
                buf.extend(chunk)
                last_recv_t = time.monotonic()
                got_data = True
                logger.info(  # 临时 INFO 级（诊断期，等 barcode 稳定后改回 DEBUG）
                    f"← scanner recv {len(chunk)}B {bytes(chunk)!r}  buf_len={len(buf)}"
                )
            except socket.timeout:
                pass
            except OSError as e:
                logger.warning(f"扫码枪 recv 异常: {e}; 重连")
                self._close_sock()
                continue

            # 2) 拆 buffer 派发完整 message（按 < / 终止符切）
            self._drain_and_dispatch(buf)

            # 3) ★ stale-flush ★ buf 还剩字节 + 静默 ≥1s → 当 message 冲刷
            #    对齐 Halcon receive_data 'z' timeout fallback 行为
            if (not got_data) and buf and last_recv_t > 0.0:
                if (time.monotonic() - last_recv_t) >= stale_flush_s:
                    self._flush_stale_buffer(buf)
                    last_recv_t = 0.0

            # 4) ★看门狗（唯一逻辑）★ 有棒过、却连续 N 根没扫到码 → 半开/僵尸连接，强制重连。
            #    背景(2026-05-28 宜宾)：TCP 半开后 recv 永远走上面 `except socket.timeout: pass`、
            #    sendall('start') 也不报错，于是 _connected 一直 True、永不重连——复产后每根棒
            #    NoRead，只能人工重启。判据只看「有棒过却没码」：notify_activity 每完成一根 +1，
            #    扫到码清 0；连续 _ZOMBIE_MISS_LIMIT 根没码就重连。纯空闲(没棒)时计数不涨 → 不动它。
            if self._connected and self._rods_since_barcode >= _ZOMBIE_MISS_LIMIT:
                with self._lock:
                    missed = self._rods_since_barcode
                    self._rods_since_barcode = 0   # 重置，避免立刻再触发
                self._watchdog_reconnects_total += 1
                logger.warning(
                    f"[看门狗] 连续 {missed} 根棒检测完成却没扫到码（半开/僵尸连接），"
                    f"强制重连。累计看门狗重连={self._watchdog_reconnects_total}"
                )
                self._close_sock()
                continue

            # 5) 周期 keepalive trigger（对齐 Halcon Rec_Code:7354）
            now = time.monotonic()
            if (now - last_trigger_t) >= self.poll_interval_s:
                if self._send_trigger():
                    last_trigger_t = now

            # 6) 心跳
            self._maybe_heartbeat()

        logger.info("扫码枪循环已退出")
        self._close_sock()
