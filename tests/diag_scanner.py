"""
扫码枪长时间诊断（独立可跑，不依赖项目其他模块）
====================================================
用途：你不在现场没法手动放棒，需要"长时间挂着监听"看扫码枪到底有没有 push 出来。
脚本本身不依赖项目 import，只用 Python 标准库 → 拷一个 .py 到现场就能跑。

三种模式：

  --mode halcon       连接 → init 时 send 1 次 b"start\\x00" → 之后只听不发
                      （iter49 简化版：跟现网 Halcon 行为相同，但不再 re-send start。
                      用来看 "init 1 次 start 后扫码枪能不能持续 push"）

  --mode silent       连接 → **完全不发任何字节** → 只 recv
                      （用来看 firmware 是否就处于 auto-push 模式：
                      TriSrc=5 (TCP) + TriMode=1 应该等 trigger 才扫；
                      但是 if firmware 是 free-running，silent 也会收到数据）

  --mode poll N       连接 → init send start → 每 N 秒 re-send 一次 start
                      （iter48 风格，对照用）

输出：
  - 屏幕：每 30s 心跳，每次 recv 完整 dump bytes（hex + ascii）
  - 文件：tests/outputs/diag_scanner_YYYYMMDD_HHMMSS.log（完整日志，长跑必看）

跑法（PowerShell）：

    # 现场 5000 端口（注意：要先把老 Halcon 停掉！只能一个 TCP 连）
    python tests\\diag_scanner.py --host 192.168.12.56 --port 5000 --mode halcon

    # 不发任何东西，纯听 10 分钟看 firmware 会不会自己 push
    python tests\\diag_scanner.py --mode silent --duration 600

    # 高频 poll 对照
    python tests\\diag_scanner.py --mode poll --poll-interval 2

诊断决策树：
  - silent 模式收到字节 → firmware 是 auto-push 模式，根本不需要 send start，
    那 iter48/iter49 的 send 'start' 反而可能是干扰
  - silent 没收到 + halcon 模式收到 → 'start' 是必需的触发，iter49 路子对
  - halcon 模式也没收到 → 不是协议问题。检查：a) 真的有棒经过吗 b) 老 Halcon 是
    不是真的停了 c) firmware Get 状态对不对
  - poll 模式 N=2 频繁发也没收到 → firmware 状态有问题或线被拔
"""

from __future__ import annotations

import argparse
import datetime
import logging
import socket
import sys
import threading
import time
from pathlib import Path

_NULL = b"\x00"


# ─────────── 日志双输出（屏幕 + 文件）───────────

def _setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("DiagScanner")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # 屏幕：INFO 以上
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    # 文件：DEBUG 全部
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.addHandler(fh)
    logger.propagate = False
    return logger


# ─────────── byte dump 工具 ───────────

def _dump_bytes(b: bytes, max_len: int = 64) -> str:
    """hex + ascii，方便看协议字节"""
    head = b[:max_len]
    hex_str = " ".join(f"{x:02X}" for x in head)
    ascii_str = "".join(chr(x) if 32 <= x < 127 else "." for x in head)
    tail = f" ...(+{len(b) - max_len}B)" if len(b) > max_len else ""
    return f"hex=[{hex_str}] ascii='{ascii_str}'{tail}"


# ─────────── 主诊断循环 ───────────

def diag(host: str, port: int, mode: str, poll_interval_s: float,
         duration_s: float, log_file: Path) -> int:
    log = _setup_logging(log_file)

    log.info("=" * 70)
    log.info(f"扫码枪诊断启动 mode={mode}")
    log.info(f"target = {host}:{port}")
    log.info(f"poll_interval = {poll_interval_s}s  (mode=poll 才用)")
    log.info(f"duration = {duration_s}s")
    log.info(f"log_file = {log_file}")
    log.info("=" * 70)

    sock: socket.socket | None = None

    def _try_connect() -> bool:
        nonlocal sock
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            s.settimeout(2.0)
            s.connect((host, port))
            s.settimeout(1.0)
            sock = s
            log.info(f"[OK] 已连接 {host}:{port} "
                     f"(local={s.getsockname()})")
            return True
        except Exception as e:
            log.warning(f"[FAIL] 连接 {host}:{port} 失败: {e}")
            return False

    def _send_start() -> bool:
        if sock is None:
            return False
        try:
            sock.sendall(b"start" + _NULL)
            log.info("→ SEND b'start\\x00' (6 bytes)")
            return True
        except Exception as e:
            log.warning(f"→ SEND 失败: {e}")
            return False

    if not _try_connect():
        log.error("初始连接失败 → 退出")
        return 2

    # 模式相关：init 时是否 send start
    if mode in ("halcon", "poll"):
        _send_start()
    elif mode == "silent":
        log.info("[silent] 不发任何字节，纯监听 firmware 是否会 auto-push")
    else:
        log.error(f"未知 mode={mode}")
        return 1

    # 统计
    t0 = time.monotonic()
    last_hb = t0
    last_poll = t0
    total_bytes = 0
    total_barcodes = 0
    window_bytes = 0
    window_barcodes = 0
    window_timeouts = 0
    buf = bytearray()

    try:
        while True:
            now = time.monotonic()
            elapsed = now - t0
            if elapsed >= duration_s:
                log.info(f"达到 duration={duration_s}s → 退出")
                break

            # poll 模式定时 re-send
            if mode == "poll" and (now - last_poll) >= poll_interval_s:
                _send_start()
                last_poll = now

            # recv 1s timeout
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    log.warning("recv 返回空 → 对端 FIN")
                    break
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                log.info(f"← RECV {len(chunk)}B @ {ts}  {_dump_bytes(chunk)}")
                buf.extend(chunk)
                total_bytes += len(chunk)
                window_bytes += len(chunk)
            except socket.timeout:
                window_timeouts += 1
            except OSError as e:
                log.warning(f"recv 异常: {e}")
                break

            # 拆 NUL
            while _NULL in buf:
                idx = buf.find(_NULL)
                msg = bytes(buf[:idx])
                del buf[:idx + 1]
                s_str = msg.decode("ascii", errors="replace").strip()
                if s_str:
                    total_barcodes += 1
                    window_barcodes += 1
                    log.info(f"  ★ BARCODE #{total_barcodes}: '{s_str}'")

            # 30s 心跳
            if (now - last_hb) >= 30.0:
                log.info(
                    f"[heartbeat] elapsed={elapsed:.0f}s "
                    f"window: bytes={window_bytes} barcodes={window_barcodes} "
                    f"timeouts={window_timeouts} "
                    f"| total: bytes={total_bytes} barcodes={total_barcodes}"
                )
                window_bytes = 0
                window_barcodes = 0
                window_timeouts = 0
                last_hb = now

    except KeyboardInterrupt:
        log.info("[Ctrl+C] 用户中断")

    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        log.info("=" * 70)
        log.info(f"[END] 总计 recv {total_bytes} bytes, {total_barcodes} barcodes")
        log.info(f"      持续 {time.monotonic() - t0:.1f}s")
        log.info(f"      完整日志: {log_file}")
        log.info("=" * 70)
        if total_barcodes == 0:
            log.warning(
                ">>> 一根棒都没扫到。可能性：\n"
                "    1) 老 Halcon 占着端口（如果是同一台机器跑诊断，确认 Halcon 已停）\n"
                "    2) 现场真没棒经过（去看产线传送带）\n"
                "    3) firmware 状态错（用 <Get,TriSrc> 等命令核对）\n"
                "    4) PLC 硬件触发线断（扫码枪即使 TriSrc=5 TCP 也可能等 PLC 信号）"
            )

    return 0 if total_barcodes > 0 else 3


def main() -> int:
    ap = argparse.ArgumentParser(
        description="扫码枪诊断脚本（独立可跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--host", default="192.168.12.56")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--mode", choices=("halcon", "silent", "poll"),
                    default="halcon",
                    help="halcon = init send start 1 次后只听；"
                         "silent = 完全不发只听；"
                         "poll = 定时 re-send start")
    ap.add_argument("--poll-interval", type=float, default=5.0,
                    help="poll 模式重发周期秒")
    ap.add_argument("--duration", type=float, default=1800.0,
                    help="跑多少秒后自动退出（默认 30 分钟，Ctrl+C 也行）")
    ap.add_argument("--log-dir", default=None,
                    help="日志目录（默认 tests/outputs/）")
    args = ap.parse_args()

    log_dir = (Path(args.log_dir) if args.log_dir
               else Path(__file__).resolve().parent / "outputs")
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"diag_scanner_{args.mode}_{ts}.log"

    return diag(args.host, args.port, args.mode,
                args.poll_interval, args.duration, log_file)


if __name__ == "__main__":
    sys.exit(main())
