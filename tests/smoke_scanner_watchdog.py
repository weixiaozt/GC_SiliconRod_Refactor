"""
扫码枪看门狗 smoke test（僵尸连接自愈，且空闲不误伤）
=========================================================
复现 2026-05-28 宜宾现场故障：扫码枪 TCP **连着、但不再回任何数据**（半开 / 僵尸连接）。
这种状态下 ``recv`` 永远超时、``sendall('start')`` 又不报错，旧逻辑里 ``_connected`` 会
一直 True、永不重连——复产后每根棒都 NoRead，只能人工重启软件才恢复。

★ 看门狗只有一条逻辑（计数式，阈值 _ZOMBIE_MISS_LIMIT 写死在代码里、不进 config）：
    有棒过（notify_activity 被调）、却【连续 N 根】没扫到码 → 判僵尸连接、强制重连；
    扫到码就把计数清 0；纯空闲（没棒过）notify_activity 不被调 → 计数不涨 → 永不重连。

本测试用"哑服务器"（只收不回）分两段验证：
  阶段 1（僵尸）：周期调用 notify_activity（= 模拟有棒在过）→ 看门狗【应】重连。
  阶段 2（真空闲）：【不】调用 notify_activity（= 没棒过）→ 看门狗【绝不】重连。
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
from sirod_inspector.core.scanner_client import _ZOMBIE_MISS_LIMIT


class MuteScannerServer:
    """哑扫码枪：accept + 读掉触发，但【从不回数据】（模拟半开 / 僵尸连接）。"""

    def __init__(self):
        self.host = "127.0.0.1"
        self.port = 0
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.conn_count = 0   # 被建立的连接数（每次看门狗重连后 +1）

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(5)
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
                self.conn_count += 1
                cid = self.conn_count
                print(f"  [mute-server] accept 连接 #{cid}（只收不回，模拟僵尸连接）")
                try:
                    conn.settimeout(0.5)
                    while not self._stop.is_set():
                        try:
                            b = conn.recv(64)
                            if not b:
                                break
                        except socket.timeout:
                            continue
                        except OSError:
                            break
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

        self._thread = threading.Thread(target=loop, daemon=True, name="MuteScanner")
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


def _pump_activity(client: ScannerClient, seconds: float, interval: float = 0.4) -> None:
    """在 seconds 秒内每 interval 秒喂一次 notify_activity（模拟产线一根根出棒）。"""
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        client.notify_activity()
        time.sleep(interval)


def main() -> int:
    print("=" * 64)
    print(f"ScannerClient 看门狗 smoke test（阈值 = 连续 {_ZOMBIE_MISS_LIMIT} 根没码）")
    print("=" * 64)

    server = MuteScannerServer()
    server.start()
    print(f"mute server: {server.host}:{server.port}")

    client = ScannerClient(
        host=server.host, port=server.port,
        poll_interval_s=0.5,
        recv_timeout_s=0.3,
        reconnect_interval_s=0.5,
    )
    client.start()
    time.sleep(1.0)   # 先连上

    # ── 阶段 1：有棒在过(喂 activity) + 扫不到码 → 连续 N 根触发重连 ──
    print(f"\n[阶段 1] 有棒在过却扫不到码：每 0.4s 喂一次 notify_activity，共 ~5s")
    wd0 = client.stats_snapshot()["watchdog_reconnects_total"]
    _pump_activity(client, seconds=5.0, interval=0.4)   # ~12 根 → 阈值5 → ~2 次重连
    time.sleep(0.5)   # 让最后可能 pending 的一次重连落定
    wd1 = client.stats_snapshot()["watchdog_reconnects_total"]
    fired = wd1 - wd0
    print(f"  阶段1 看门狗重连 = {fired}（期望 ≥2）")

    # ── 阶段 2：没棒过(停喂 activity) + 扫不到码 → 绝不重连 ──
    print("\n[阶段 2] 停线空闲(没棒过)：停喂 notify_activity 4s")
    time.sleep(4.0)
    wd2 = client.stats_snapshot()["watchdog_reconnects_total"]
    idle_fired = wd2 - wd1
    print(f"  阶段2 看门狗重连 = {idle_fired}（期望 0 —— 空闲绝不重连）")

    print("\n[阶段 3] 停止")
    client.stop()
    server.stop()

    ok = (fired >= 2) and (idle_fired == 0)
    print("\n[OK] 看门狗验证通过：僵尸会自愈、空闲不误伤" if ok
          else f"\n[FAIL] 未达预期（阶段1={fired} 应≥2，阶段2={idle_fired} 应=0）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
