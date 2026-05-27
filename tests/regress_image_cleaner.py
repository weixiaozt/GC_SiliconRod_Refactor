"""
回归测试：ImageCleaner 差异化保留 + dry_run
============================================
造不同 mtime 的图，验证：
  - TIF / OK 全图 短保留（7天）→ 老的删
  - NG 全图 / crops / WebImage 长保留（30天）→ 10天前的留、40天前的删
  - 新文件（1天）一律留
  - dry_run=True 只统计不真删

跑：uv run python tests/regress_image_cleaner.py
"""
import os, sys, time, tempfile, shutil
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "sirod_inspector"))
from core.image_cleaner import ImageCleaner

root = tempfile.mkdtemp(prefix="cleaner_test_")
base = os.path.join(root, "images")
tif  = os.path.join(root, "ImageRaw")
web  = os.path.join(root, "WebImage")

def mk(path, days_ago):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * 1024)
    t = time.time() - days_ago * 86400
    os.utime(path, (t, t))

# retain tif=7 / ok_full=7 / ng_full=30 / crops=30 / web=30
mk(os.path.join(tif, "old.tif"),     10)   # → 删
mk(os.path.join(tif, "recent.tif"),   1)   # → 留
mk(os.path.join(base, "d1", "full", "raw",    "OK", "old.bmp"),    10)  # → 删
mk(os.path.join(base, "d2", "full", "raw",    "OK", "recent.bmp"),  1)  # → 留
mk(os.path.join(base, "d1", "full", "marked", "NG", "old_ng.png"), 10)  # → 留(30)
mk(os.path.join(base, "d1", "crops", "raw", "old_crop.bmp"),       40)  # → 删(30)
mk(os.path.join(web, "old.png"), 10)       # → 留(30)

cfg = {"enabled": True, "dry_run": True,
       "retain_days": {"tif": 7, "ok_full": 7, "ng_full": 30, "crops": 30, "webimage": 30}}

s = ImageCleaner(base_dir=base, raw_tif_dir=tif, web_image_dir=web, cleanup_cfg=cfg).cleanup()
print(f"[DRY-RUN] 将删 {s.deleted} 个 / {s.freed_mb:.2f}MB  {s.by_class}")
assert os.path.exists(os.path.join(tif, "old.tif")), "dry_run 竟然真删了！"

cfg["dry_run"] = False
s2 = ImageCleaner(base_dir=base, raw_tif_dir=tif, web_image_dir=web, cleanup_cfg=cfg).cleanup()
print(f"[REAL] 删了 {s2.deleted} 个 / {s2.freed_mb:.2f}MB  {s2.by_class}")

checks = [
    (os.path.join(tif, "old.tif"),                                    False, "TIF老(7天阈值)"),
    (os.path.join(tif, "recent.tif"),                                 True,  "TIF新"),
    (os.path.join(base, "d1", "full", "raw", "OK", "old.bmp"),        False, "OK老(7天)"),
    (os.path.join(base, "d2", "full", "raw", "OK", "recent.bmp"),     True,  "OK新"),
    (os.path.join(base, "d1", "full", "marked", "NG", "old_ng.png"),  True,  "NG老(30天保留)"),
    (os.path.join(base, "d1", "crops", "raw", "old_crop.bmp"),        False, "crop老(40>30删)"),
    (os.path.join(web, "old.png"),                                    True,  "web老(30天保留)"),
]
ok = True
for path, should_exist, desc in checks:
    exists = os.path.exists(path)
    good = (exists == should_exist)
    ok = ok and good
    print(f"  [{'OK' if good else 'FAIL'}] {desc}: 期望{'留' if should_exist else '删'}, 实际{'留' if exists else '删'}")

shutil.rmtree(root, ignore_errors=True)
print("=== 全部通过 ===" if ok else "=== 有 FAIL ===")
sys.exit(0 if ok else 1)
