"""
软件授权锁冒烟/回归
====================
不依赖现场硬件、不依赖编译，纯走 license_guard.check_license 的判定逻辑：

    有效 / 临期预警 / 过期硬停 / 签名篡改 / 换机不匹配 /
    容忍1项硬件变化 / 永久授权 / 缺授权文件 / 防改钟水位线

跑::
    uv run python tests/smoke_license_guard.py

全过打印 [ALL PASS] 退 0；任一断言失败退 1。
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sirod_inspector.core import license_guard as lg  # noqa: E402

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

_fails = 0


def check(cond: bool, label: str) -> None:
    global _fails
    mark = "OK " if cond else "X  "
    print(f"  [{mark}] {label}")
    if not cond:
        _fails += 1


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    return priv, pub_hex


def _sign_license(priv, anchors, expires_at, warn_days=15, customer="测试"):
    payload = {
        "v": 1, "anchors": anchors, "expires_at": expires_at,
        "warn_days": warn_days, "customer": customer,
        "issued_at": date.today().isoformat(),
    }
    sig = priv.sign(lg._canonical(payload)).hex()
    return {"payload": payload, "sig": sig}


def _write(tmp: Path, doc: dict) -> str:
    p = tmp / "license.dat"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def main() -> int:
    priv, pub_hex = _keypair()
    _, other_pub_hex = _keypair()
    ANCHORS = ["board:aaaa111122223333", "cpu:bbbb444455556666", "disk:cccc777788889999"]

    # 机器锚点固定成 ANCHORS（不读真实硬件，测试确定性）
    lg.machine_anchors = lambda: list(ANCHORS)  # type: ignore[assignment]
    lg._anchors_cache = list(ANCHORS)

    tmp = Path(tempfile.mkdtemp(prefix="sirod_lic_"))

    print("授权锁判定逻辑:")

    # 1. 有效（一年后到期，锚点全中）
    lp = _write(tmp, _sign_license(priv, ANCHORS, (date.today() + timedelta(days=365)).isoformat()))
    s = lg.check_license(pubkey_hex=pub_hex, license_path=lp)
    check(s.ok and s.code == "OK" and not s.warn, f"有效授权 → OK (剩{s.days_left}天)")

    # 2. 临期预警（剩 5 天，warn_days=15）
    lp = _write(tmp, _sign_license(priv, ANCHORS, (date.today() + timedelta(days=5)).isoformat()))
    s = lg.check_license(pubkey_hex=pub_hex, license_path=lp)
    check(s.ok and s.warn and s.days_left == 5, f"临期 → OK+warn (剩{s.days_left}天)")

    # 3. 过期硬停（昨天到期）
    lp = _write(tmp, _sign_license(priv, ANCHORS, (date.today() - timedelta(days=1)).isoformat()))
    s = lg.check_license(pubkey_hex=pub_hex, license_path=lp)
    check((not s.ok) and s.code == "EXPIRED", f"过期 → 拒 ({s.code})")

    # 4. 签名篡改（签完改 payload 的到期日）
    doc = _sign_license(priv, ANCHORS, (date.today() + timedelta(days=10)).isoformat())
    doc["payload"]["expires_at"] = (date.today() + timedelta(days=9999)).isoformat()
    lp = _write(tmp, doc)
    s = lg.check_license(pubkey_hex=pub_hex, license_path=lp)
    check((not s.ok) and s.code == "BAD_SIGNATURE", f"改到期日 → 验签失败 ({s.code})")

    # 5. 换了公钥（别人的私钥签的，本机公钥验不过）
    lp = _write(tmp, _sign_license(priv, ANCHORS, (date.today() + timedelta(days=365)).isoformat()))
    s = lg.check_license(pubkey_hex=other_pub_hex, license_path=lp)
    check((not s.ok) and s.code == "BAD_SIGNATURE", f"他人私钥签 → 验签失败 ({s.code})")

    # 6. 换机不匹配（授权绑的是另一台机器的锚点）
    other_anchors = ["board:dead0000", "cpu:beef1111", "disk:face2222"]
    lp = _write(tmp, _sign_license(priv, other_anchors, (date.today() + timedelta(days=365)).isoformat()))
    s = lg.check_license(pubkey_hex=pub_hex, license_path=lp)
    check((not s.ok) and s.code == "MACHINE_MISMATCH", f"换机 → 拒 ({s.code})")

    # 7. 容忍 1 项硬件变化（授权 3 锚点，本机只剩 2 个匹配 + 1 个变了）
    changed = [ANCHORS[0], ANCHORS[1], "disk:NEWDISK9999"]
    lp = _write(tmp, _sign_license(priv, changed, (date.today() + timedelta(days=365)).isoformat()))
    s = lg.check_license(pubkey_hex=pub_hex, license_path=lp)
    check(s.ok and s.code == "OK", "换1块盘(3中2命中) → 仍放行")

    # 8. 永久授权（expires_at=null，仅绑机器）
    lp = _write(tmp, _sign_license(priv, ANCHORS, None))
    s = lg.check_license(pubkey_hex=pub_hex, license_path=lp)
    check(s.ok and s.code == "OK" and s.days_left is None, "永久授权 → OK")

    # 9. 缺授权文件
    s = lg.check_license(pubkey_hex=pub_hex, license_path=str(tmp / "nope.dat"))
    check((not s.ok) and s.code == "NO_LICENSE", f"无 license.dat → 拒 ({s.code})")

    # 10. 占位公钥（没配公钥）
    s = lg.check_license(pubkey_hex=lg._PUBKEY_PLACEHOLDER, license_path=lp)
    check((not s.ok) and s.code == "NO_PUBKEY", f"未配公钥 → 拒 ({s.code})")

    # 11. 防改钟：水位线在未来 → 取水位线当今天，未到期的也算过期
    print("防改钟水位线:")
    saved_wm = lg._read_watermark()
    try:
        future = date.today() + timedelta(days=200)
        lg._write_watermark(future)
        eff = lg._effective_today()
        check(eff == future, f"系统今天被‘记录的最晚日期’盖过 → 用 {future}")
        # 一个剩 100 天的授权，在水位线(未来200天)下应判过期
        lp = _write(tmp, _sign_license(priv, ANCHORS, (date.today() + timedelta(days=100)).isoformat()))
        s = lg.check_license(pubkey_hex=pub_hex, license_path=lp)
        check((not s.ok) and s.code == "EXPIRED", f"改钟回拨也偷不到时间 → 仍判过期 ({s.code})")
    finally:
        # 还原水位线，别污染本机后续真实运行
        if saved_wm is not None:
            lg._write_watermark(saved_wm)
        else:
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, lg._WM_SUBKEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, lg._WM_VALUE)
            except Exception:
                pass

    print()
    if _fails == 0:
        print("[ALL PASS] 授权锁判定逻辑全部通过")
        return 0
    print(f"[FAIL] {_fails} 项断言未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
