"""
海康 MV-ID 扫码枪本地 mock 服务器
====================================
模拟现场扫码枪行为，让你不去工厂也能验证 iter54 的 ScannerClient：

  - 监听本地 TCP 端口（默认 5000）
  - 收到任何客户端发的字节都不管（接受 "start" 触发但不依赖）
  - 两种 push 模式：
      ★ 交互模式（--interactive）★：按 Enter / 输入棒号触发，对齐现场"先扫码再转编码器"的流程
      自动模式：周期 push barcode（--interval N 秒）
  - 默认 push 裸字符串无终止符（复刻现场实测），可改 --mode null/crlf/data/mixed

用法（开发机本地两个终端）::

    # 终端 1 - 交互模式（推荐，模拟全套流程）
    python tests/mock_hikvision_scanner.py --interactive
    # 然后在 mock 终端：
    #   输入 XJN2604BB0221C0-6W-1 + Enter → 立即 push 这个棒号
    #   空 Enter → push 默认列表里下一根
    #   q + Enter → 退出

    # 终端 1 - 自动模式（5s 一根，无需交互）
    python tests/mock_hikvision_scanner.py --interval 5

    # 终端 2 - main_camera UI（用盐城本地克隆 config）
    copy sirod_inspector\\config.yancheng-local.json sirod_inspector\\config.json
    python sirod_inspector\\main_camera.py

支持的 push 模式（--mode）::

  raw     ← 默认。push 裸字符串无终止符（最接近现场实测，验 iter54 stale-flush）
  null    ← push 字符串 + b"\\x00"（Halcon 'z' 协议风格）
  crlf    ← push 字符串 + b"\\r\\n"
  data    ← push b"<Data,XJN...>" 包裹格式
  mixed   ← 4 种格式轮转，全面 stress test parser
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))
try:
    from _test_utils import setup_console_utf8
    setup_console_utf8()
except ImportError:
    pass


_MOCK_BARCODES = [
    "XJN2604BB0221C0-6W-1",
    "XJN2605CC1171C0-5",
    "XJN2604BB0931W0-4",
    "XJN2605DD0011A2-3",
    "XJN2604EE0772B1-7",
]


def _encode_message(barcode: str, mode: str, idx: int) -> bytes:
    """按 mode 把 barcode 字符串编码成实际推回去的字节"""
    if mode == "mixed":
        actual = ["raw", "null", "crlf", "data"][idx % 4]
    else:
        actual = mode

    if actual == "raw":
        return barcode.encode("ascii")
    if actual == "null":
        return barcode.encode("ascii") + b"\x00"
    if actual == "crlf":
        return barcode.encode("ascii") + b"\r\n"
    if actual == "data":
        return f"<Data,{barcode}>".encode("ascii")
    raise ValueError(f"未知 mode: {mode}")


def _drain_socket_bg(conn: socket.socket, stop_flag: threading.Event,
                      addr) -> None:
    """后台线程：吃掉 client 发来的 trigger 字节并 log（不阻塞 push 节奏）"""
    conn.settimeout(0.3)
    while not stop_flag.is_set():
        try:
            data = conn.recv(4096)
            if not data:
                print(f"\n  [-] client FIN: {addr}")
                stop_flag.set()
                return
            print(f"\n  ← recv {len(data)}B {data!r}")
            print("  push barcode> ", end="", flush=True)
        except socket.timeout:
            continue
        except (OSError, ConnectionError) as e:
            print(f"\n  [-] client error: {e}")
            stop_flag.set()
            return


def serve_one_client_auto(conn: socket.socket, addr,
                          interval_s: float, mode: str, max_pushes: int) -> None:
    """自动模式：吃 incoming 字节 + 周期 push barcode"""
    print(f"  [+] client connected: {addr}")
    conn.settimeout(0.2)
    push_idx = 0
    next_push_t = time.monotonic() + interval_s
    total_recv = 0

    try:
        while True:
            try:
                data = conn.recv(4096)
                if not data:
                    print(f"  [-] client FIN: {addr} (total recv={total_recv}B)")
                    return
                total_recv += len(data)
                print(f"  ← recv {len(data)}B {data!r}")
            except socket.timeout:
                pass
            except (OSError, ConnectionError) as e:
                print(f"  [-] client error: {e}")
                return

            now = time.monotonic()
            if now >= next_push_t:
                barcode = _MOCK_BARCODES[push_idx % len(_MOCK_BARCODES)]
                msg = _encode_message(barcode, mode, push_idx)
                try:
                    conn.sendall(msg)
                    print(
                        f"  → push #{push_idx + 1}: {msg!r}  "
                        f"({len(msg)}B, mode='{mode}', barcode='{barcode}')"
                    )
                except (OSError, ConnectionError) as e:
                    print(f"  [-] push failed: {e}")
                    return
                push_idx += 1
                next_push_t = now + interval_s

                if max_pushes > 0 and push_idx >= max_pushes:
                    print(f"  [i] 达到 max_pushes={max_pushes}，断连")
                    return
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve_one_client_interactive(conn: socket.socket, addr, mode: str) -> None:
    """交互模式：读 stdin 行，每次 Enter push 一根 barcode（用户控制节奏）"""
    print(f"\n  [+] client connected: {addr}")
    print()
    print("  ★ 交互模式 ★")
    print("    输入棒号 + Enter   → 立即 push 这个棒号")
    print(f"    直接 Enter         → push 默认列表下一根 ({_MOCK_BARCODES[0]} ...)")
    print("    q + Enter          → 断开 client 并退出")
    print()

    stop_flag = threading.Event()
    drain_thread = threading.Thread(
        target=_drain_socket_bg, args=(conn, stop_flag, addr), daemon=True,
        name=f"DrainSocket-{addr[1]}",
    )
    drain_thread.start()

    push_idx = 0
    try:
        while not stop_flag.is_set():
            try:
                line = input("  push barcode> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  [Ctrl+C / EOF] 退出")
                return
            if line.lower() == "q":
                print("  [q] 主动断开")
                return
            if not line:
                barcode = _MOCK_BARCODES[push_idx % len(_MOCK_BARCODES)]
            else:
                barcode = line
            msg = _encode_message(barcode, mode, push_idx)
            try:
                conn.sendall(msg)
                print(
                    f"  → push #{push_idx + 1}: {msg!r}  "
                    f"({len(msg)}B, mode='{mode}', barcode='{barcode}')"
                )
                push_idx += 1
            except (OSError, ConnectionError) as e:
                print(f"  [-] push failed: {e}")
                return
    finally:
        stop_flag.set()
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(
        description="海康 MV-ID 扫码枪本地 mock",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址（默认 0.0.0.0 任意网卡）")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--interval", type=float, default=8.0,
                    help="模拟棒经过频率，秒（默认 8s 一根）")
    ap.add_argument("--mode", default="raw",
                    choices=("raw", "null", "crlf", "data", "mixed"),
                    help="push 格式（默认 raw = 现场实测格式 19 字节无终止符）")
    ap.add_argument("--max-pushes", type=int, default=0,
                    help="每个 client 最多 push 多少根后主动断连（0=不限）")
    ap.add_argument("--interactive", action="store_true",
                    help="交互模式：按 Enter / 输入棒号触发 push（推荐！对齐现场流程）")
    args = ap.parse_args()

    print("=" * 60)
    print(f"  Mock 海康扫码枪")
    print(f"  监听:     {args.host}:{args.port}")
    if args.interactive:
        print(f"  模式:     ★ 交互（按 Enter / 输入棒号触发）★")
    else:
        print(f"  模式:     自动 ({args.interval}s/根)")
    print(f"  push 格式: {args.mode}")
    if not args.interactive:
        print(f"  max_pushes:{args.max_pushes if args.max_pushes else 'unlimited'}")
    print("=" * 60)
    print("等 ScannerClient 连接... (Ctrl+C 退出)")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    # 交互模式只允许一个 client（避免多个 client 抢同一个 stdin）
    srv.listen(2 if not args.interactive else 1)
    srv.settimeout(0.5)

    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break

            if args.interactive:
                # 交互模式直接在主线程处理（占用 stdin）
                serve_one_client_interactive(conn, addr, args.mode)
                # 一个 client 完事后回到 accept 循环（等待下一次连接）
            else:
                # 自动模式起独立线程，支持多 client 并发
                t = threading.Thread(
                    target=serve_one_client_auto,
                    args=(conn, addr, args.interval, args.mode, args.max_pushes),
                    daemon=True, name=f"MockHikvisionClient-{addr[1]}",
                )
                t.start()
    except KeyboardInterrupt:
        print("\n[Ctrl+C] 退出")
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
