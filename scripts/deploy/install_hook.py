r"""
方案 B 一键安装器 —— 把「site 钩子锁」装进《当前这个 python》的 site-packages。
==============================================================================
★ 必须用"实际启动 main_camera.py 的那个 python"来跑本脚本 ★
（launcher 用 PATH 上的 pythonw/python；不确定就先 `where python` 看一眼。）

    python scripts\deploy\install_hook.py             # 安装
    python scripts\deploy\install_hook.py --uninstall # 卸载（删 site-packages 里那 3 个文件）

做的事：
  1. 按本 python 版本自动挑对应的 license_guard.<abi>.pyd（3.10→cp310 / 3.11→cp311…）
  2. 挑一个★可写★的 site-packages（系统目录只读时退到用户级，Store 版 python 必需）
  3. 把  license_guard.pyd + sirod_license_hook.py + sirod_lock.pth  拷进去
  4. pip install cryptography，自检并打印当前授权状态
查找 .pyd 的位置：脚本同目录 → 仓库 build\ 下 → tools\（hook/.pth 在 tools\）。
"""

from __future__ import annotations

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


def _candidate_site_dirs() -> list:
    dirs = []
    try:
        dirs += [Path(p) for p in site.getsitepackages()]
    except Exception:  # noqa: BLE001
        pass
    try:
        u = site.getusersitepackages()
        if u:
            dirs.append(Path(u))
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), []
    for p in dirs:
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def _can_write(p: Path) -> bool:
    try:
        if not p.exists():
            return False
        t = p / ".sirod_wtest.tmp"
        t.write_text("x", encoding="ascii")
        t.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def _user_site() -> Path | None:
    try:
        u = site.getusersitepackages()
        return Path(u) if u else None
    except Exception:  # noqa: BLE001
        return None


def _pick_site_packages():
    """挑一个★可写★的 site-packages。返回 (路径, 是否用户级)。

    Microsoft Store 版 python 的系统 site-packages 在 Program Files\\WindowsApps 下是
    只读的，会退到用户级（AppData 下，可写；必要时创建）。
    """
    user = _user_site()
    user_str = str(user) if user else None
    cands = _candidate_site_dirs()
    # 1) 可写、名为 site-packages 的系统目录优先（锁对该 python 全局生效）
    for p in cands:
        if p.name == "site-packages" and str(p) != user_str and _can_write(p):
            return p, False
    # 2) 任意可写候选
    for p in cands:
        if _can_write(p):
            return p, (str(p) == user_str)
    # 3) 用户级，建好再用
    if user is not None:
        try:
            user.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        if _can_write(user):
            return user, True
    raise RuntimeError("找不到可写的 site-packages —— 试试以管理员身份运行本脚本，"
                       "或改用普通 python（非 Microsoft Store 版）。")


def _find(name: str) -> Path | None:
    here = Path(__file__).resolve().parent
    cands = [here / name, ROOT / "tools" / name, *ROOT.glob(f"build/**/{name}")]
    for c in cands:
        if c.is_file():
            return c
    return None


def _uninstall() -> int:
    # 在所有候选 site-packages（系统 + 用户级）里删，确保装哪都能撤干净
    n = 0
    seen = set()
    for sp in _candidate_site_dirs():
        for f in list(sp.glob("license_guard.*.pyd")) + [sp / HOOK_NAME, sp / PTH_NAME]:
            if f.is_file() and str(f) not in seen:
                try:
                    f.unlink()
                    print(f"  删 {f}")
                    n += 1
                    seen.add(str(f))
                except Exception as e:  # noqa: BLE001
                    print(f"  [!] 删不掉 {f}: {e}")
    print(f"[OK] 已卸载 {n} 个文件，锁已撤（license.dat 可留可删）。")
    return 0


def main() -> int:
    print("=" * 64)
    print(f"  方案B 安装器 | python {sys.version.split()[0]} ({TAG})")
    print(f"  目标 python : {sys.executable}")

    if "--uninstall" in sys.argv:
        print("=" * 64)
        return _uninstall()

    try:
        sp, is_user = _pick_site_packages()
    except RuntimeError as e:
        print(f"[X] {e}")
        return 1
    print(f"  site-packages: {sp}")
    if is_user:
        print("  (用户级 site-packages —— Store/无管理员时的可写位置；锁对‘这个用户启动 app’"
              "生效，单用户工控机没问题)")
    print("=" * 64)

    pyd, hook, pth = _find(PYD_NAME), _find(HOOK_NAME), _find(PTH_NAME)
    missing = []
    if not pyd:
        missing.append(f"{PYD_NAME}  ← 本 python 是 {TAG}，需要这个版本的锁；"
                       f"放脚本同目录或 build\\ 下")
    if not hook:
        missing.append(HOOK_NAME + "  ← 应在 tools\\ 或脚本同目录")
    if not pth:
        missing.append(PTH_NAME + "  ← 应在 tools\\ 或脚本同目录")
    if missing:
        print("[X] 缺少文件：")
        for m in missing:
            print("   -", m)
        return 1

    try:
        shutil.copy2(pyd, sp / PYD_NAME)
        shutil.copy2(hook, sp / HOOK_NAME)
        shutil.copy2(pth, sp / PTH_NAME)
    except PermissionError as e:
        print(f"[X] 拷贝被拒（{e}）—— 该 site-packages 不可写。"
              "请以管理员身份重跑，或改用普通 python。")
        return 1
    print(f"[OK] 已拷入：{PYD_NAME} / {HOOK_NAME} / {PTH_NAME}")

    print("[*] pip install cryptography ...")
    r = subprocess.run([sys.executable, "-m", "pip", "install",
                        "-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "cryptography"])
    if r.returncode != 0:
        print("[!] cryptography 自动安装失败，请手动：pip install cryptography")

    print("[*] 自检 ...")
    try:
        import license_guard
        st = license_guard.check_license()
        print(f"    锁组件就位；当前授权状态：{st.code} — {st.message}")
        if st.code == "NO_LICENSE":
            print("    （正常：还没放 license.dat。签发后放到 app 启动目录/项目根即可。）")
    except Exception as e:  # noqa: BLE001
        print(f"[!] 自检 import 失败：{e}（确认 cryptography 已装、.pyd 版本与本 python 一致）")

    print("=" * 64)
    print("  装好了。接下来：")
    print("   1) 取机器码（装锁后可直接用）：")
    print('      python -c "import license_guard; print(license_guard.fingerprint_blob())"')
    print("   2) 把机器码发开发签发 license.dat，放 app 启动目录（项目根）")
    print("   3) 正常启动 app；★必测：把 license.dat 改名后启动应起不来★")
    print("   撤销：python scripts\\deploy\\install_hook.py --uninstall")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
