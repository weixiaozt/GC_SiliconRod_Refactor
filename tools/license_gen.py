"""
SiRod 授权签发工具（厂商侧，★ 永不进现场 / 永不进 git ★）
============================================================
持有 *私钥*，给指定机器签发 ``license.dat``。现场只跑编译后的 license_guard.pyd
（内嵌 *公钥*）验签——私钥一旦泄露整套授权作废，务必离线保管。

只依赖 ``cryptography`` + 标准库，可单独拷出来用（不依赖项目其它代码）。

用法
----
1) 生成一次性密钥对（只做一次，私钥留好）::

       python tools/license_gen.py keygen
       # → 写出 private_key.pem，并打印要粘进 license_guard.py 的 _PUBLIC_KEY_HEX 行

2) 现场发来机器码 blob 后，签发授权::

       python tools/license_gen.py issue --machine <blob> --expires 2027-05-31
       python tools/license_gen.py issue --machine <blob> --days 365 --customer "盐城-1线"
       python tools/license_gen.py issue --machine <blob> --expires none   # 永久授权(仅绑机器)
       # → 写出 license.dat，发回现场放到项目根目录

3) 查看一个 license.dat 的内容（不验签，只看字段）::

       python tools/license_gen.py inspect --in license.dat
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date, timedelta
from pathlib import Path


# ── 与 license_guard._canonical 必须逐字节一致 ──
def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _load_private_key(path: Path):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(path.read_bytes(), password=None)


def cmd_keygen(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"[X] {out} 已存在。覆盖会作废所有已签发授权！确认请加 --force", file=sys.stderr)
        return 1

    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out.write_bytes(pem)

    pub = priv.public_key()
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_hex = pub_raw.hex()

    print("=" * 64)
    print(f"  私钥已写出: {out.resolve()}")
    print("  ★ 离线保管，永不进 git / 现场。泄露 = 所有授权作废 ★")
    print("=" * 64)
    print("  把下面这一行覆盖到 sirod_inspector/core/license_guard.py，再编译 .pyd：")
    print()
    print(f'_PUBLIC_KEY_HEX = "{pub_hex}"')
    print()
    return 0


def _decode_machine(blob: str) -> dict:
    raw = base64.b64decode(blob.strip().encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def _resolve_expiry(expires, days):
    """解析到期参数 → (expires_at|None, err|None)。

    expires_at=None 表示永久授权；err 非 None 表示参数有误（调用方打印并退出）。
    """
    if expires is not None and days is not None:
        return None, "--expires 和 --days 二选一"
    if days is not None:
        return (date.today() + timedelta(days=int(days))).isoformat(), None
    if expires is not None:
        if expires.lower() in ("none", "perpetual", "永久"):
            return None, None
        try:
            return date.fromisoformat(expires).isoformat(), None
        except ValueError:
            return None, f"--expires 日期格式应为 YYYY-MM-DD: {expires!r}"
    return None, "必须指定 --expires <YYYY-MM-DD|none> 或 --days <N>"


def cmd_issue(args: argparse.Namespace) -> int:
    key_path = Path(args.key)
    if not key_path.is_file():
        print(f"[X] 私钥不存在: {key_path}（先跑 keygen）", file=sys.stderr)
        return 1

    try:
        machine = _decode_machine(args.machine)
        anchors = list(machine.get("anchors") or [])
        host = machine.get("host", "?")
    except Exception as e:  # noqa: BLE001
        print(f"[X] 机器码 blob 解析失败: {e}", file=sys.stderr)
        return 1

    if not anchors:
        print("[!] 警告：该机器没有可用硬件锚点 —— 授权将不绑机器（防拷能力大幅下降）")
        if not args.force:
            print("    确认仍要签发请加 --force", file=sys.stderr)
            return 1

    # ── 到期日 ──
    expires_at, err = _resolve_expiry(args.expires, args.days)
    if err:
        print(f"[X] {err}", file=sys.stderr)
        return 1

    payload = {
        "v": 1,
        "anchors": anchors,
        "expires_at": expires_at,
        "warn_days": int(args.warn_days),
        "customer": args.customer or host,
        "issued_at": date.today().isoformat(),
    }

    priv = _load_private_key(key_path)
    sig = priv.sign(_canonical(payload)).hex()

    doc = {"payload": payload, "sig": sig}
    out = Path(args.out)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 64)
    print(f"  授权已签发: {out.resolve()}")
    print(f"  客户/机器 : {payload['customer']}  (host={host})")
    print(f"  到期      : {expires_at or '永久（仅绑机器）'}")
    print(f"  绑定锚点  : {len(anchors)} 个")
    print(f"  临期提醒  : 到期前 {payload['warn_days']} 天")
    print("=" * 64)
    print("  把 license.dat 发回现场，放到项目根目录即可。")
    return 0


def cmd_renew(args: argparse.Namespace) -> int:
    """给现有 license.dat 续期/改永久 —— 机器锚点不变，无需现场重新取机器码。"""
    key_path = Path(args.key)
    if not key_path.is_file():
        print(f"[X] 私钥不存在: {key_path}（先跑 keygen）", file=sys.stderr)
        return 1
    try:
        # utf-8-sig：容忍现场回传的 license.dat 带 BOM
        old = json.loads(Path(args.infile).read_text(encoding="utf-8-sig"))
        payload = dict(old["payload"])
    except Exception as e:  # noqa: BLE001
        print(f"[X] 读旧授权失败 {args.infile}: {e}", file=sys.stderr)
        return 1

    expires_at, err = _resolve_expiry(args.expires, args.days)
    if err:
        print(f"[X] {err}", file=sys.stderr)
        return 1

    payload["expires_at"] = expires_at
    payload["issued_at"] = date.today().isoformat()
    if args.warn_days is not None:
        payload["warn_days"] = int(args.warn_days)

    priv = _load_private_key(key_path)
    sig = priv.sign(_canonical(payload)).hex()
    out = Path(args.out)
    out.write_text(json.dumps({"payload": payload, "sig": sig},
                              ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 64)
    print(f"  续期完成: {out.resolve()}")
    print(f"  机器/客户: {payload.get('customer', '?')}")
    print(f"  新到期   : {expires_at or '永久（仅绑机器）'}")
    print(f"  绑定锚点 : {len(payload.get('anchors') or [])} 个（不变，无需现场重新取码）")
    print("=" * 64)
    print("  把新 license.dat 发回现场覆盖旧的即可。")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    doc = json.loads(Path(args.infile).read_text(encoding="utf-8-sig"))
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="SiRod 授权签发工具（厂商私钥侧）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pk = sub.add_parser("keygen", help="生成 Ed25519 密钥对")
    pk.add_argument("--out", default="private_key.pem", help="私钥输出路径")
    pk.add_argument("--force", action="store_true", help="允许覆盖已有私钥（危险）")
    pk.set_defaults(func=cmd_keygen)

    pi = sub.add_parser("issue", help="给指定机器签发 license.dat")
    pi.add_argument("--machine", required=True, help="现场 get_machine_id.py 打印的机器码 blob")
    pi.add_argument("--expires", default=None, help="到期日 YYYY-MM-DD，或 none=永久")
    pi.add_argument("--days", default=None, help="从今天起 N 天后到期（与 --expires 二选一）")
    pi.add_argument("--warn-days", default=15, help="到期前几天开始弹提醒")
    pi.add_argument("--customer", default=None, help="客户/机器备注（写进授权，可读）")
    pi.add_argument("--key", default="private_key.pem", help="私钥路径")
    pi.add_argument("--out", default="license.dat", help="授权输出路径")
    pi.add_argument("--force", action="store_true", help="无锚点也强行签发")
    pi.set_defaults(func=cmd_issue)

    prn = sub.add_parser("renew", help="给现有 license.dat 续期/改永久（机器码不变，无需现场重新取码）")
    prn.add_argument("--in", dest="infile", default="license.dat", help="旧授权文件")
    prn.add_argument("--expires", default=None, help="新到期日 YYYY-MM-DD，或 none=永久")
    prn.add_argument("--days", default=None, help="从今天起 N 天后到期（与 --expires 二选一）")
    prn.add_argument("--warn-days", default=None, help="改临期提醒天数（默认沿用旧值）")
    prn.add_argument("--key", default="private_key.pem", help="私钥路径")
    prn.add_argument("--out", default="license.dat", help="新授权输出路径")
    prn.set_defaults(func=cmd_renew)

    px = sub.add_parser("inspect", help="查看 license.dat 字段（不验签）")
    px.add_argument("--in", dest="infile", default="license.dat")
    px.set_defaults(func=cmd_inspect)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
