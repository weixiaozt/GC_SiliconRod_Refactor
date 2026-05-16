"""
扫码枪客户端 smoke test（带 mock 服务器）
==========================================
本地起一个伪扫码枪 TCP 服务器：
  - 监听 127.0.0.1:随机端口
  - 收到 "start\\0" 请求 → 回应一个棒号 + "\\0"
  - 按 Halcon ``Code_Tcp`` 协议格式

然后启 ``ScannerClient`` 连过去：
  - 验证能收到棒号
  - 验证 ``current_rod_id()`` 线程安全
  - 验证连接断开后自动重连
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

from sirod_inspector.core import ScannerClient


# ============================================================
# Mock 服务器：Halcon 'z' 协议
# ============================================================

class MockScannerServer:
    """伪扫码枪。每次收到 'start\\0' 回应预设的棒号。"""

    def __init__(self, rod_ids):
        self.rod_ids = list(rod_ids)
        self.idx = 0
        self.host = "127.0.0.1"
        self.port = 0          # 让 OS 分配
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        # 控制：是否在某次请求时主动断开（测试重连）
        self.drop_after_n_requests = -1
        self._req_count = 0

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(1)
        self.port = s.getsockname()[1]
        self._sock = s

        def loop():
            while not self._stop.is_set():
                try:
                    s.settimeout(0.5)
                    conn, _ = s.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    conn.settimeout(0.5)
                    while not self._stop.is_set():
                        # 读取一个 'z' 字符串
                        buf = bytearray()
                        try:
                            while not self._stop.is_set():
                                b = conn.recv(1)
                                if not b:
                                    raise ConnectionError("client gone")
                                if b == b"\x00":
                                    break
                                buf.extend(b)
                        except socket.timeout:
                            continue
                        except ConnectionError:
                            break
                        req = buf.decode("ascii", errors="replace")
                        if req != "start":
                            print(f"  [mock-server] 未预期请求: {req!r}")
                            continue
                        self._req_count += 1
                        # 模拟掉连
                        if (self.drop_after_n_requests > 0
                                and self._req_count >= self.drop_after_n_requests):
                            print(f"  [mock-server] 主动断开 (#{self._req_count})")
                            self.drop_after_n_requests = -1
                            break
                        # 回应一个棒号
                        rod_id = self.rod_ids[self.idx % len(self.rod_ids)]
                        self.idx += 1
                        conn.sendall(rod_id.encode("ascii") + b"\x00")
                        print(f"  [mock-server] req#{self._req_count} → {rod_id!r}")
                except Exception as e:
                    print(f"  [mock-server] conn 异常: {e}")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            try:
                s.close()
            except Exception:
                pass

        self._thread = threading.Thread(target=loop, daemon=True,
                                         name="MockScanner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=3.0)


# ============================================================
# 测试
# ============================================================

def main() -> int:
    print("=" * 60)
    print("ScannerClient smoke test (with mock server)")
    print("=" * 60)

    rod_ids = ["ROD_A_001", "ROD_A_002", "ROD_A_003",
               "ROD_B_001", "ROD_B_002"]

    server = MockScannerServer(rod_ids)
    server.start()
    print(f"mock server: {server.host}:{server.port}")

    client = ScannerClient(
        host=server.host, port=server.port,
        poll_interval_s=0.5,        # 更快用于测试
        recv_timeout_s=0.3,
        reconnect_interval_s=0.5,
    )

    # ──── 阶段 1：正常收 3 个 ────
    print("\n[阶段 1] 正常接收 3 个棒号")
    client.start()
    received = []
    for _ in range(20):
        time.sleep(0.3)
        rod = client.current_rod_id()
        if rod and rod != "NoRead" and (not received or received[-1] != rod):
            received.append(rod)
        if len(received) >= 3:
            break
    print(f"  收到: {received}")

    # ──── 阶段 2：服务器主动断开 → 客户端自动重连 ────
    print("\n[阶段 2] 模拟服务器掉连，验证自动重连")
    pre_count = server._req_count
    server.drop_after_n_requests = pre_count + 2
    time.sleep(4.0)
    post_count = server._req_count
    print(f"  请求数: {pre_count} -> {post_count}（重连后继续）")
    rod_after_reconnect = client.current_rod_id()
    print(f"  重连后最新棒号: {rod_after_reconnect!r}")

    # ──── 阶段 3：关闭 ────
    print("\n[阶段 3] 停止")
    client.stop()
    server.stop()

    ok = (len(received) >= 3
          and post_count > pre_count + 2
          and rod_after_reconnect != "NoRead")
    print("\n[OK] scanner client 验证通过" if ok
          else "[FAIL] scanner client 未达预期")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
