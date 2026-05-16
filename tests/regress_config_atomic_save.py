"""
config.save() 原子写测试
==========================
之前 save() 用 open(path, "w") + json.dump：流式写、不原子。
如果进程在写一半时挂（断电 / Ctrl-C），config.json 会残缺，
下次启动 JSON 解析失败 → 软件起不来。

修复后：写 path.tmp + fsync + os.replace。任意时刻 path 要么是
旧版本（替换前）要么是新版本（替换后），不会有半写状态。

注意：AppConfig 是单例（__new__ 模式），不能 new 多份。
测试用 json.load 直接读盘验证持久化，不依赖第二份 AppConfig。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

from sirod_inspector.data.config import AppConfig


_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  {'[OK ]' if cond else '[FAIL]'} {name}"
           + (f"  — {detail}" if detail else ""))
    if not cond:
        _fail += 1


def _reset_singleton():
    """绕过单例 — 测试用"""
    AppConfig._instance = None


def main() -> int:
    print("=" * 60)
    print("config.save() 原子性 / 往返测试")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "subdir", "config.json")

        # 1) 基础保存：write to nested path, dir auto-create
        _reset_singleton()
        cm = AppConfig(cfg_path)
        cm.set("test.key", "value1")
        cm.set("test.nested.deep", 42)
        cm.set("test.list", [1, 2, 3])
        cm.save()
        check("config.json 已生成（含父目录自建）", os.path.exists(cfg_path))

        # 2) 直接 json.load 验证持久化（不走 AppConfig 单例）
        with open(cfg_path, "r", encoding="utf-8") as f:
            disk = json.load(f)
        check("disk key=value1", disk.get("test", {}).get("key") == "value1")
        check("disk nested=42",
              disk.get("test", {}).get("nested", {}).get("deep") == 42)
        check("disk list=[1,2,3]",
              disk.get("test", {}).get("list") == [1, 2, 3])

        # 3) 原子性：写后 .tmp 文件不应残留
        tmp_path = cfg_path + ".tmp"
        check(".tmp 已清理", not os.path.exists(tmp_path))

        # 4) CJK 中文不被 escape（json.dump ensure_ascii=False）
        cm.set("中文.key", "正常中文值")
        cm.save()
        raw = Path(cfg_path).read_text(encoding="utf-8")
        check("CJK 直存（不 \\u escape）", "正常中文值" in raw)

        # 5) 重复保存覆盖（同一文件原地更新）
        cm.set("test.key", "value2")
        cm.save()
        with open(cfg_path, "r", encoding="utf-8") as f:
            disk2 = json.load(f)
        check("重复保存覆盖", disk2.get("test", {}).get("key") == "value2")

        # 6) 模拟"中间损坏"再加载 — 写残缺 json，下次重启用默认值
        # （AppConfig.load 捕获 json 异常 → 用 _DEFAULT_CONFIG）
        Path(cfg_path).write_text(
            '{"test": {"key": "broken json no closing brace',
            encoding="utf-8")
        _reset_singleton()
        cm_broken = AppConfig(cfg_path)
        # load 不抛，返回默认值；"test.key" 应该是默认或 None
        # （重点是不 crash）
        check("坏 JSON 不导致加载崩溃", cm_broken is not None)

        # 7) 加载正常 JSON
        Path(cfg_path).write_text(
            '{"test": {"key": "fromdisk"}}', encoding="utf-8")
        _reset_singleton()
        cm_good = AppConfig(cfg_path)
        check("从磁盘加载好 JSON",
              cm_good.get("test.key") == "fromdisk")

    print()
    if _fail == 0:
        print("[OK] config.save() 原子性 / 往返全通")
        return 0
    print(f"[FAIL] {_fail} 个失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
