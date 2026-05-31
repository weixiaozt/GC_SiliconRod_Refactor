r"""
方案 B 一键安装器 —— 把「site 钩子锁」装进《当前这个 python》的 site-packages。
==============================================================================
★ 必须用"实际启动 main_camera.py 的那个 python"来跑本脚本 ★
（launcher 用 PATH 上的 pythonw/python；不确定就先 `where python` 看一眼。）

    python scripts\deploy\install_hook.py             # 安装
    python scripts\deploy\install_hook.py --uninstall # 卸载（删 site-packages 里那 3 个文件）

做的事：
  1. 按本 python 版本自动挑对应的 license_guard.<abi>.pyd（3.10→cp310 / 3.11→cp311…）
  2. 把  license_guard.pyd + sirod_license_hook.py + sirod_lock.pth  拷进 site-packages
  3. pip install cryptography
  4. 自检并打印当前授权状态
查找 .pyd 的位置：脚本同目录 → 仓库 build\ 下 → tools\（hook/.pth 在 tools\）。
所以既能在仓库里直接跑，也能把"脚本+3个文件"打成一个便携文件夹拷到现场跑。
"""

from __future__ import annotations

import os
import shutil
import site
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAG = f"cp{sys.version_info.major}{sys.version_info.minor}"
PYD_NAME = f"license_guard.{TAG}-win_amd64.pyd"
HOOK_NAME = "sirod_license_hook.py"
PTH_NAME = "sirod_lock.pth"


def _site_packages() -> Path:
    cands = []
    try:
        cands += list(site.getsitepackages())
    except Exception:  # noqa: BLE001
        pass
    try:
        cands.append(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    for c in cands:
        p = Path(c)
        if p.name == "site-packages" and p.exists():
            return p
    for c in cands:
        if Path(c).exists():
            return Path(c)
    raise RuntimeError("找不到 site-packages")


def _find(name: str) -> Path | None:
    here = Path(__file__).resolve().parent
    cands = [here / name, ROOT / "tools" / name, *ROOT.glob(f"build/**/{name}")]
    for c in cands:
        if c.is_file():
            return c
    return None


def _uninstall(sp: Path) -> int:
    n = 0
    for f in list(sp.glob("license_guard.*.pyd")) + [sp / HOOK_NAME, sp / PTH_NAME]:
        if f.is_file():
            f.unlink()
            print(f"  删 {f.name}")
            n += 1
    print(f"[OK] 已卸载 {n} 个文件，锁已撤（license.dat 可留可删）。")
    return 0


def main() -> int:
    print("=" * 64)
    print(f"  方案B 安装器 | python {sys.version.split()[0]} ({TAG})")
    print(f"  目标 python : {sys.executable}")
    sp = _site_packages()
    print(f"  site-packages: {sp}")
    print("=" * 64)

    if "--uninstall" in sys.argv:
        return _uninstall(sp)

    pyd = _find(PYD_NAME)
    hook = _find(HOOK_NAME)
    pth = _find(PTH_NAME)
    missing = []
    if not pyd:
        missing.append(f"{PYD_NAME}  ← 本 python 是 {TAG}，需要这个版本的锁；"
                       f"放脚本同目录或 build\\ 下（先编好对应版本的 .pyd）")
    if not hook:
        missing.append(HOOK_NAME)
    if not pth:
        missing.append(PTH_NAME)
    if missing:
        print("[X] 缺少文件：")
        for m in missing:
            print("   -", m)
        return 1

    shutil.copy2(pyd, sp / PYD_NAME)
    shutil.copy2(hook, sp / HOOK_NAME)
    shutil.copy2(pth, sp / PTH_NAME)
    print(f"[OK] 已拷入 site-packages：{PYD_NAME} / {HOOK_NAME} / {PTH_NAME}")

    print("[*] pip install cryptography ...")
    r = subprocess.run([sys.executable, "-m", "pip", "install",
                        "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "cryptography"])
    if r.returncode != 0:
        print("[!] cryptography 自动安装失败，请手动：pip install cryptography")

    print("[*] 自检 ...")
    try:
        import license_guard  # 刚拷进 site-packages，本进程可直接 import
        st = license_guard.check_license()
        print(f"    锁组件就位；当前授权状态：{st.code} — {st.message}")
        if st.code == "NO_LICENSE":
            print("    （正常：还没放 license.dat。签发后放到项目根目录即可。）")
    except Exception as e:  # noqa: BLE001
        print(f"[!] 自检 import 失败：{e}")
        print("    （确认 cryptography 已装、且 .pyd 版本与本 python 一致）")

    print("=" * 64)
    print("  装好了。接下来：")
    print("   1) python tools\\get_machine_id.py   → 机器码发开发签发 license.dat")
    print("   2) 把 license.dat 放项目根目录")
    print("   3) 正常启动 app；★必测：把 license.dat 改名后启动应起不来★")
    print("   撤销：python scripts\\deploy\\install_hook.py --uninstall")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
