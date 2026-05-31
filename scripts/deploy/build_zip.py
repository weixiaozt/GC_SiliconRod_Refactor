"""
打部署 zip — 精准排除 logs / pycache / 老 lock 文件

用法::
    python scripts/deploy/build_zip.py           # 全包：带 EasyLabel runtime（~155MB）
    python scripts/deploy/build_zip.py --lite    # 轻量：不带 runtime（~33MB，已装过 D:\\EasyLabel_x64 可用）
"""

import zipfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "SiliconRod_v2_deploy.zip"

# tier B：编了授权锁 .pyd 就只发二进制、排除明文 license_guard.py（见 should_exclude）
LOCK_PYD_EXISTS = any((ROOT / "sirod_inspector" / "core").glob("license_guard*.pyd"))

INCLUDE_DIRS = ["sirod_inspector", "models", "scripts"]
# get_machine_id.py：现场取机器码用（license_gen.py 是厂商私钥侧工具，★不打进现场★）
INCLUDE_FILES = ["DEPLOY.md", "tools/get_machine_id.py"]

# EasyLabel AI runtime —— 现场缺 dnninfer.dll / dnndefine.dll 时必带
# ★ 不要用 MvitSDK_4.1.23.622.exe 安装器，那个之前实测装不出来 ★
# 直接用预打包 EasyLabel_DL_runtime.zip 解压到 D:\ 就有 D:\EasyLabel_x64\DeepLearning\ 全部 DLL
SDK_FILES = [
    "EasyLabel_DL_runtime.zip",
]


def should_exclude(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/")
    parts = rel.split("/")
    for p in parts:
        if p in ("__pycache__", ".git"):
            return True
        if p.endswith(".pyc") or p.endswith(".pyo"):
            return True
    if rel.startswith("sirod_inspector/logs"):
        return True
    if "main_camera.lock" in rel:
        return True
    if rel.endswith(".tmp"):
        return True
    # dev-only 运行时数据 — 部署机必须重新生成，否则带过去会污染统计 / 泄密
    if rel == "sirod_inspector/config.json":
        return True
    if rel == "sirod_inspector/shift_stats.json":
        return True
    # ── 授权锁 ──
    # 私钥 / 任意 .pem 绝不进现场包
    if rel.endswith(".pem"):
        return True
    # 每台机器单独签发的授权文件不打包（现场手动放对应那台的）
    if parts[-1] == "license.dat":
        return True
    # tier B：编了 .pyd 就只发 .pyd，排除明文锁源码（否则现场能看到/删掉锁逻辑，白编）
    if LOCK_PYD_EXISTS and rel == "sirod_inspector/core/license_guard.py":
        return True
    return False


def main():
    lite = "--lite" in sys.argv

    count, total_bytes = 0, 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d in INCLUDE_DIRS:
            base = ROOT / d
            if not base.is_dir():
                print(f"WARN: 目录不存在 {d}")
                continue
            for fp in base.rglob("*"):
                if not fp.is_file():
                    continue
                rel = fp.relative_to(ROOT).as_posix()
                if should_exclude(rel):
                    continue
                z.write(fp, rel)
                count += 1
                total_bytes += fp.stat().st_size

        for f in INCLUDE_FILES:
            fp = ROOT / f
            if not fp.is_file():
                print(f"WARN: 文件不存在 {f}")
                continue
            z.write(fp, f)
            count += 1
            total_bytes += fp.stat().st_size

        # SDK 安装包（默认带，--lite 跳过）
        if not lite:
            for f in SDK_FILES:
                fp = ROOT / f
                if not fp.is_file():
                    print(f"WARN: SDK 文件不存在 {f}")
                    continue
                size_mb = fp.stat().st_size / 1024 / 1024
                print(f"打包 runtime: {f}  ({size_mb:.0f} MB)")
                z.write(fp, f)
                count += 1
                total_bytes += fp.stat().st_size

    zip_size = OUT.stat().st_size
    print()
    print(f"打包完成: {OUT}")
    print(f"  模式: {'轻量（不带 EasyLabel runtime）' if lite else '全包（带 EasyLabel_DL_runtime）'}")
    print(f"  文件数: {count}")
    print(f"  原始大小: {total_bytes/1024/1024:.1f} MB")
    print(f"  zip 大小: {zip_size/1024/1024:.1f} MB "
          f"(压缩比 {(1 - zip_size/total_bytes)*100:.0f}%)")

    # ── 授权锁打包状态（醒目，防忘编 .pyd 把明文锁发出去 / 漏锁）──
    print()
    if LOCK_PYD_EXISTS:
        print("  授权锁: [OK] 已打包 license_guard.pyd（强制生效），已排除明文 .py")
    else:
        print("  授权锁: [!!] 未发现 license_guard.pyd —— 包里是明文 .py，现场会以")
        print("         『源码宽松模式』运行 = 等于没锁！要启用锁先编译再重新打包：")
        print("         scripts\\deploy\\build_license_guard.bat")
    if not lite:
        print()
        print("现场部署：")
        print("  1) 解压 zip → D:\\SiliconRod_v2\\")
        print("  2) 解压 EasyLabel_DL_runtime.zip → 直接到 D:\\ 根目录（产出 D:\\EasyLabel_x64\\）")
        print("  3) 双击 scripts\\deploy\\check_deps.bat 看红绿灯")
        print("  4) 双击 scripts\\deploy\\新版本.bat 启动")


if __name__ == "__main__":
    sys.exit(main())
