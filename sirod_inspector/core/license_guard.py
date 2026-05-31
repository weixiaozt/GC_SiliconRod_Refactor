"""
软件授权闸门 (license guard)
============================
启动时校验「本机是否被授权运行 + 是否在有效期内」，挡住未授权拷贝/到期不续。

设计要点
--------
- **非对称签名 (Ed25519)**：本模块只内嵌 *公钥*，签发授权用的 *私钥* 永远不进
  现场、不进 git。所以即便整份源码暴露，也伪造不出有效授权。
- **绑机器**：用主板/CPU/BIOS/磁盘序列号算「机器锚点」，授权里写死锚点集合，
  换机/整盘拷到别的机器 → 锚点对不上 → 拒。容忍 1 项硬件变化（修机换盘不至于直接砖）。
- **防改钟**：到期日可以靠「把系统时间调回去」绕过。这里在 HKCU 存一个「见过的
  最晚日期」水位线，取 max(系统今天, 水位线) 当「今天」，改钟偷不到时间。
- **编译保护 (tier B)**：本文件用 ``nuitka --module`` 编译成 ``license_guard.pyd``
  发到现场（删掉同名 .py）。验签逻辑 + 公钥藏在二进制里，不是记事本能改的明文。

  ``__compiled__`` 是 Nuitka 给编译模块注入的标记：
    - **编译态 (.pyd, 生产)** → 永远严格，校验不过就拦。
    - **源码态 (.py, 开发机)** → 默认放行 + 打 WARNING，免得开发自己被锁死。
      （生产只发 .pyd，所以这条宽松不会削弱现场。）
    - 想在源码态测严格逻辑：设环境变量 ``SIROD_LICENSE_STRICT=1``，或直接调
      :func:`check_license` 看返回值（测试用，不弹框不退出）。

运维流程（详见 docs/LICENSING.md）
----------------------------------
1. 你（厂商）跑一次 ``python tools/license_gen.py keygen`` → 得私钥 PEM + 公钥；
   把公钥粘到下面 ``_PUBLIC_KEY_HEX``，再编译成 .pyd。
2. 现场跑 ``python tools/get_machine_id.py`` → 打印「机器码 blob」，发给你。
3. 你 ``python tools/license_gen.py issue --machine <blob> --expires 2027-05-31``
   → 产出 ``license.dat``，发回现场放到项目根。
4. 启动即校验。

闸门位置：``main_camera.main()`` 最前面调 :func:`verify_or_exit`。
失败时弹原生框后 ``sys.exit(0)`` —— 退 0 让 launcher 当「正常退出」，**不会**触发
watchdog 的 5 次重启刷屏（见 scripts/deploy/launcher.py 的退出码约定）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger("SiRod.License")

# ─────────────────────────────────────────────────────────────────────────
# 配置常量
# ─────────────────────────────────────────────────────────────────────────

# 内嵌公钥（Ed25519 原始公钥 32 字节的 hex）。运行 tools/license_gen.py keygen
# 生成后，把它打印的整行 _PUBLIC_KEY_HEX = "..." 覆盖到这里，再编译成 .pyd。
# ★ 留着占位符 = 还没配公钥 ★ —— 编译态会因此拒绝放行（防忘配）。

_PUBLIC_KEY_HEX = "b3a0b29ad30b9926b1860c447b9c5d7a2852c7b9f5c995d34485772ce4493702"
_PUBKEY_PLACEHOLDER = "PASTE_PUBLIC_KEY_HEX_HERE"

# license.dat 候选位置：环境变量 > 项目根 > sirod_inspector/。取第一个存在的。
_LICENSE_ENV = "SIROD_LICENSE"
_LICENSE_FILENAME = "license.dat"

# 临期提醒：剩余天数 <= 该值时弹「还剩 X 天」提醒框（但放行）。授权里可覆盖。
_WARN_DAYS_DEFAULT = 15

# 机器锚点匹配：授权里 N 个锚点，现场至少匹配 (N-1) 个才算同一台机器（容忍 1 项变化）。
# 同时硬性要求至少匹配 1 个，避免 N=1 时阈值变成 0。
def _required_matches(total: int) -> int:
    return max(1, total - 1)

# 改钟容差（天）：系统时间比水位线早超过这么多，才记一条 tamper 警告（不影响判定，
# 判定一律用 max(系统今天, 水位线)）。
_CLOCK_SLACK_DAYS = 2

# 改钟水位线存放位置（HKCU，普通权限可写；藏在不起眼的子键里）。
_WM_SUBKEY = r"Software\SiRod\Runtime"
_WM_VALUE = "rt"

# 硬件序列号里要当成「无效」剔除的厂商占位垃圾值（小写比较）。
_JUNK_HW_VALUES = {
    "", "none", "null", "0", "00000000", "ffffffff", "00000000000000000000",
    "default string", "to be filled by o.e.m.", "to be filled by o.e.m",
    "system serial number", "system serial", "filled by oem", "not specified",
    "not applicable", "not available", "n/a", "na", "oem", "o.e.m.",
    "无", "序列号", "00 00 00 00 00 00 00 00",
}


# ─────────────────────────────────────────────────────────────────────────
# 机器锚点（硬件指纹）
# ─────────────────────────────────────────────────────────────────────────

_anchors_cache: Optional[list] = None


def _is_compiled() -> bool:
    """是否为 Nuitka 编译态（.pyd）。生产=True，源码运行=False。"""
    return "__compiled__" in globals()


def _is_junk_hw(value: Optional[str]) -> bool:
    if value is None:
        return True
    return value.strip().lower() in _JUNK_HW_VALUES


def _run_powershell(command: str, timeout: float = 20.0) -> Optional[str]:
    """跑一段 PowerShell 返回 stdout（去尾换行）。失败返回 None。

    用 CREATE_NO_WINDOW 防止 pythonw 下闪控制台。非 Windows / 无 powershell 时返回 None。
    """
    if os.name != "nt":
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout, creationflags=flags,
        )
    except Exception as e:  # noqa: BLE001 — 硬件读取任何异常都不该让闸门崩
        logger.debug(f"PowerShell 调用失败: {e}")
        return None
    if proc.returncode != 0:
        logger.debug(f"PowerShell 非零退出 rc={proc.returncode}: {proc.stderr.strip()[:200]}")
        return None
    return (proc.stdout or "").strip()


def _collect_hw() -> dict:
    """一次 PowerShell 调用拿全部硬件序列号（主板/CPU/BIOS/磁盘）。"""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$b=(Get-CimInstance Win32_BaseBoard).SerialNumber;"
        "$c=(Get-CimInstance Win32_Processor | Select-Object -First 1).ProcessorId;"
        "$i=(Get-CimInstance Win32_BIOS).SerialNumber;"
        "$d=(Get-CimInstance Win32_DiskDrive | Select-Object -First 1).SerialNumber;"
        "[pscustomobject]@{board=$b;cpu=$c;bios=$i;disk=$d} | ConvertTo-Json -Compress"
    )
    out = _run_powershell(ps)
    if not out:
        return {}
    try:
        data = json.loads(out)
        return {k: (str(v).strip() if v is not None else "") for k, v in data.items()}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"解析硬件信息失败: {e}")
        return {}


def _mac_anchor() -> Optional[str]:
    """主网卡 MAC 当兜底锚点。getnode 拿不到真实 MAC（返回随机多播位）时跳过。"""
    node = uuid.getnode()
    if (node >> 40) & 1:  # 第 8 位是多播位 → uuid 没拿到真 MAC，是随机值
        return None
    return f"{node:012x}"


def _hash_anchor(name: str, raw: str) -> str:
    """单个锚点 → "name:前16位sha256"。hash 一下避免明文序列号外泄到 license.dat。"""
    h = hashlib.sha256(raw.strip().lower().encode("utf-8")).hexdigest()[:16]
    return f"{name}:{h}"


def machine_anchors() -> list:
    """本机的机器锚点列表（已排序、去垃圾）。进程内缓存，避免重复调 PowerShell。

    返回形如 ``["board:ab12...", "cpu:cd34...", ...]``。可能为空（硬件全读不到）。
    """
    global _anchors_cache
    if _anchors_cache is not None:
        return _anchors_cache

    anchors = []
    hw = _collect_hw()
    for name in ("board", "cpu", "bios", "disk"):
        v = hw.get(name, "")
        if not _is_junk_hw(v):
            anchors.append(_hash_anchor(name, v))

    mac = _mac_anchor()
    if mac:
        anchors.append(_hash_anchor("mac", mac))

    _anchors_cache = sorted(set(anchors))
    logger.debug(f"机器锚点: {_anchors_cache}")
    return _anchors_cache


def fingerprint_blob() -> str:
    """打包本机锚点成一行 base64 blob（给现场复制、发回厂商签发用）。"""
    payload = {
        "v": 1,
        "anchors": machine_anchors(),
        "host": _safe_hostname(),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _safe_hostname() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return "unknown"


def print_fingerprint() -> None:
    """现场用：打印机器码 blob。get_machine_id.py 调它。"""
    anchors = machine_anchors()
    print("=" * 60)
    print("  SiRod 本机授权信息（发给开发签发授权用）")
    print("=" * 60)
    print(f"  机器名: {_safe_hostname()}")
    print(f"  锚点数: {len(anchors)}")
    if len(anchors) < 2:
        print("  [!] 警告：可读硬件锚点不足 2 个，绑定较弱（可能虚拟机/工控机屏蔽了 WMI）")
    print()
    print("  ----- 复制下面这一整行发给开发 -----")
    print(fingerprint_blob())
    print("  ------------------------------------")
    print()


# ─────────────────────────────────────────────────────────────────────────
# 改钟水位线（HKCU 注册表）
# ─────────────────────────────────────────────────────────────────────────

def _read_watermark() -> Optional[date]:
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WM_SUBKEY) as key:
            val, _ = winreg.QueryValueEx(key, _WM_VALUE)
        return date.fromisoformat(str(val))
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"读水位线失败: {e}")
        return None


def _write_watermark(d: date) -> None:
    if os.name != "nt":
        return
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WM_SUBKEY) as key:
            winreg.SetValueEx(key, _WM_VALUE, 0, winreg.REG_SZ, d.isoformat())
    except Exception as e:  # noqa: BLE001
        logger.debug(f"写水位线失败: {e}")


def _effective_today() -> date:
    """防改钟的「今天」= max(系统今天, 历史见过的最晚日期)，并把水位线推进。"""
    sys_today = date.today()
    wm = _read_watermark()
    if wm is None:
        _write_watermark(sys_today)
        return sys_today
    if sys_today < wm:
        if (wm - sys_today).days > _CLOCK_SLACK_DAYS:
            logger.warning(
                f"[授权] 检测到系统时间疑似回拨：系统={sys_today} < 记录={wm}，"
                f"按记录日期计算有效期（改钟无效）"
            )
        return wm  # 用水位线，改钟偷不到时间
    # 系统今天 >= 水位线 → 推进水位线
    _write_watermark(sys_today)
    return sys_today


# ─────────────────────────────────────────────────────────────────────────
# 授权文件读取 + 验签
# ─────────────────────────────────────────────────────────────────────────

def _project_root() -> Path:
    # 本文件 sirod_inspector/core/license_guard.py → 项目根 = parents[2]
    return Path(__file__).resolve().parents[2]


def _license_candidates() -> list:
    paths = []
    env = os.environ.get(_LICENSE_ENV)
    if env:
        paths.append(Path(env))
    # 当前工作目录 —— launcher 启动 main_camera 时 cwd=项目根；site 钩子部署方式
    # （license_guard 装在 site-packages，_project_root 指不到项目）靠这个找到 license.dat。
    try:
        paths.append(Path.cwd() / _LICENSE_FILENAME)
    except Exception:  # noqa: BLE001
        pass
    # 主程序所在目录及其上一级（main_camera.py 在 sirod_inspector/ 下，项目根=其父）
    try:
        argv0 = sys.argv[0] if sys.argv else ""
        if argv0:
            appdir = Path(argv0).resolve().parent
            paths.append(appdir / _LICENSE_FILENAME)
            paths.append(appdir.parent / _LICENSE_FILENAME)
    except Exception:  # noqa: BLE001
        pass
    root = _project_root()
    paths.append(root / _LICENSE_FILENAME)
    paths.append(root / "sirod_inspector" / _LICENSE_FILENAME)
    # 去重保序
    seen, uniq = set(), []
    for p in paths:
        k = str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _find_license(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    for p in _license_candidates():
        if p.is_file():
            return p
    return None


def _canonical(payload: dict) -> bytes:
    """规范化 payload → 待签字节。gen 与 guard 必须完全一致。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _verify_signature(payload: dict, sig_hex: str, pubkey_hex: str) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(bytes.fromhex(sig_hex), _canonical(payload))
        return True
    except InvalidSignature:
        return False
    except Exception as e:  # noqa: BLE001 — 公钥格式错/sig 格式错都按验签失败处理
        logger.debug(f"验签异常: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────
# 校验结果
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class LicenseStatus:
    ok: bool
    code: str                  # OK / NO_PUBKEY / NO_CRYPTO / NO_LICENSE / BAD_FORMAT /
                               # BAD_SIGNATURE / MACHINE_MISMATCH / EXPIRED
    message: str
    days_left: Optional[int] = None
    warn: bool = False         # 临期（仍 ok），调用方弹提醒但放行


def check_license(pubkey_hex: Optional[str] = None,
                  license_path: Optional[str] = None) -> LicenseStatus:
    """纯校验：读 license.dat → 验签 → 验机器码 → 验到期。不弹框、不退出。

    供 :func:`verify_or_exit` 和测试调用。``pubkey_hex`` / ``license_path`` 仅测试时传，
    生产留空走内嵌公钥 + 自动找 license.dat。
    """
    pubkey_hex = pubkey_hex or _PUBLIC_KEY_HEX
    if pubkey_hex == _PUBKEY_PLACEHOLDER:
        return LicenseStatus(False, "NO_PUBKEY",
                             "未配置公钥（_PUBLIC_KEY_HEX 还是占位符）—— 跑 license_gen.py keygen")

    # cryptography 缺失（开发机还没 pip install）→ 单独的 code，源码态会被宽松处理
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return LicenseStatus(False, "NO_CRYPTO",
                             "缺少 cryptography 依赖：pip install cryptography")

    lic_path = _find_license(license_path)
    if lic_path is None:
        return LicenseStatus(False, "NO_LICENSE", "未找到 license.dat 授权文件")

    try:
        # utf-8-sig：容忍意外的 BOM（现场用记事本另存 license.dat 会加 BOM）。
        # 签名是对 payload 字典签的、不是原始文件字节，剥 BOM 不影响验签安全。
        doc = json.loads(lic_path.read_text(encoding="utf-8-sig"))
        payload = doc["payload"]
        sig_hex = doc["sig"]
    except Exception as e:  # noqa: BLE001
        return LicenseStatus(False, "BAD_FORMAT", f"授权文件解析失败: {e}")

    if not _verify_signature(payload, sig_hex, pubkey_hex):
        return LicenseStatus(False, "BAD_SIGNATURE", "授权文件签名无效或被篡改")

    # ── 机器码匹配 ──
    lic_anchors = set(payload.get("anchors") or [])
    if lic_anchors:
        now_anchors = set(machine_anchors())
        matched = len(lic_anchors & now_anchors)
        need = _required_matches(len(lic_anchors))
        if matched < need:
            return LicenseStatus(
                False, "MACHINE_MISMATCH",
                f"授权与本机不匹配（命中 {matched}/{len(lic_anchors)} 锚点，需 ≥{need}）"
                f"—— 是否换了机器或更换了主板/CPU/硬盘？")

    # ── 到期 ──
    expires_at = payload.get("expires_at")
    if not expires_at:
        return LicenseStatus(True, "OK", "授权有效（永久授权，仅绑机器）")

    try:
        exp = date.fromisoformat(str(expires_at))
    except ValueError:
        return LicenseStatus(False, "BAD_FORMAT", f"授权到期日格式错误: {expires_at!r}")

    today = _effective_today()
    days_left = (exp - today).days
    if days_left < 0:
        return LicenseStatus(False, "EXPIRED",
                             f"授权已于 {exp.isoformat()} 到期（已过 {-days_left} 天）",
                             days_left=days_left)

    warn_days = int(payload.get("warn_days", _WARN_DAYS_DEFAULT))
    warn = days_left <= warn_days
    return LicenseStatus(True, "OK",
                         f"授权有效，到期 {exp.isoformat()}，剩 {days_left} 天",
                         days_left=days_left, warn=warn)


# ─────────────────────────────────────────────────────────────────────────
# 原生弹框（不依赖 PyQt —— 保证 guard 能独立编译、Qt 坏了也能弹）
# ─────────────────────────────────────────────────────────────────────────

def _message_box(text: str, title: str, error: bool = True) -> None:
    # headless/测试模式：不弹框，打到 stderr（只改提示方式，不影响锁判定）
    if os.environ.get("SIROD_NO_MSGBOX") == "1":
        print(f"[{title}] {text}", file=sys.stderr)
        return
    try:
        import ctypes
        # MB_OK | (MB_ICONERROR|MB_ICONINFORMATION) | MB_SETFOREGROUND | MB_TOPMOST
        icon = 0x10 if error else 0x40
        flags = 0x0 | icon | 0x10000 | 0x40000
        ctypes.windll.user32.MessageBoxW(0, text, title, flags)
    except Exception:  # noqa: BLE001 — 非 Windows / 无 GUI 退化到 stderr
        print(f"[{title}] {text}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────
# 生产闸门
# ─────────────────────────────────────────────────────────────────────────

def verify_or_exit() -> None:
    """启动闸门。在 main_camera.main() 最前面调用。

    - 通过：静默返回（临期则先弹「还剩 X 天」提醒再返回，不阻塞）。
    - 不过：弹原生错误框后 ``sys.exit(0)``（退 0 让 launcher 当正常退出，不重启）。

    严格性：编译态(.pyd) 或设了 SIROD_LICENSE_STRICT=1 → 严格拦截；
    源码态(.py, 开发机) 默认放行 + WARNING，免得开发自己被锁。
    """
    strict = _is_compiled() or os.environ.get("SIROD_LICENSE_STRICT") == "1"
    status = check_license()

    if status.ok:
        logger.info(f"[授权] {status.message}")
        if status.warn:
            _message_box(
                f"SiRod Inspector 授权即将到期\n\n{status.message}\n\n"
                "请尽快联系开发续期，到期后将无法启动。",
                "SiRod 授权提醒", error=False)
        return

    # 校验未通过
    if not strict:
        logger.warning(
            f"[授权] 源码开发模式，校验未过仍放行（生产 .pyd 会拦截）：{status.code} {status.message}"
        )
        return

    logger.error(f"[授权] 校验未通过，拒绝启动：{status.code} {status.message}")
    _message_box(
        f"SiRod Inspector 无法启动 —— 软件授权校验未通过。\n\n"
        f"原因：{status.message}\n\n"
        "请联系开发获取/更新授权文件 (license.dat)。",
        "SiRod 授权校验失败", error=True)
    sys.exit(0)  # 退 0：launcher 视为正常退出，不触发 5 次重启


if __name__ == "__main__":
    # 源码态直接 `python -m sirod_inspector.core.license_guard` 时打印本机机器码，
    # 方便开发联调。现场用 tools/get_machine_id.py（兼容 .pyd）。
    print_fingerprint()
