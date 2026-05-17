"""
打盐城部署 zip — 精准排除 logs / pycache / 老 lock 文件
"""

import zipfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "SiliconRod_v2_deploy.zip"

INCLUDE_DIRS = ["sirod_inspector", "models", "scripts"]
INCLUDE_FILES = ["DEPLOY_yancheng.md"]


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
    return False


def main():
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

    zip_size = OUT.stat().st_size
    print(f"打包完成: {OUT}")
    print(f"  文件数: {count}")
    print(f"  原始大小: {total_bytes/1024/1024:.1f} MB")
    print(f"  zip 大小: {zip_size/1024/1024:.1f} MB "
          f"(压缩比 {(1 - zip_size/total_bytes)*100:.0f}%)")


if __name__ == "__main__":
    sys.exit(main())
