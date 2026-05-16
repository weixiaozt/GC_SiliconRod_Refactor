"""
per-class judge 单元测试（不依赖图像 / 模型）
==============================================
直接构造假的 ClassifiedDefect 喂给 judge_per_class，覆盖 5 个字段
的所有触发分支：

  report_ng / max_area / max_length / max_count / min_confidence
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

from sirod_inspector.algorithm import (
    ClassRule, ClassifiedDefect, judge_per_class,
)


# ============================================================
# 工具
# ============================================================

def mk_defect(cls_name: str, conf: float = 0.9,
              area: int = 100, radius: float = 5.0) -> ClassifiedDefect:
    return ClassifiedDefect(
        bbox=(0, 0, 10, 10), area=area, outer_radius=radius,
        class_name=cls_name, class_confidence=conf,
    )


_fail_count = 0


def check(name: str, cond: bool, detail: str = ""):
    global _fail_count
    mark = "[OK ]" if cond else "[FAIL]"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        _fail_count += 1


# ============================================================
# 测试场景
# ============================================================

def test_empty():
    print("\n── 空缺陷列表 → OK ──")
    v = judge_per_class([], [
        ClassRule(name="隐裂", report_ng=True, max_area=10),
    ])
    check("empty → is_ng=False", not v.is_ng)
    check("empty → ng_type=''", v.ng_type == "")


def test_report_ng_false():
    print("\n── report_ng=False → 即使超阈值也不报 ──")
    rules = [
        ClassRule(name="脏污", report_ng=False,
                  max_area=10, max_length=2, max_count=1),
    ]
    defects = [mk_defect("脏污", area=10000, radius=100, conf=0.99)]
    v = judge_per_class(defects, rules)
    check("脏污 area=10000 但 report_ng=False → OK", not v.is_ng)


def test_max_area():
    print("\n── max_area 触发 ──")
    rules = [ClassRule(name="隐裂", report_ng=True, max_area=10)]
    v = judge_per_class([mk_defect("隐裂", area=11)], rules)
    check("area 11 > 10 → NG", v.is_ng)
    check("ng_type='隐裂'", v.ng_type == "隐裂")

    v = judge_per_class([mk_defect("隐裂", area=10)], rules)
    check("area 10 == 10 → OK（严格 >）", not v.is_ng)


def test_max_length():
    print("\n── max_length 触发 ──")
    rules = [ClassRule(name="隐裂", report_ng=True,
                       max_area=1e9, max_length=5)]
    v = judge_per_class([mk_defect("隐裂", radius=5.1)], rules)
    check("radius 5.1 > 5 → NG", v.is_ng)


def test_min_confidence():
    print("\n── min_confidence: 置信度低不报 ──")
    rules = [ClassRule(name="隐裂", report_ng=True,
                       max_area=10, min_confidence=0.8)]
    v = judge_per_class([mk_defect("隐裂", conf=0.7, area=1000)], rules)
    check("conf 0.7 < 0.8 → OK（不够自信）", not v.is_ng)

    v = judge_per_class([mk_defect("隐裂", conf=0.85, area=1000)], rules)
    check("conf 0.85 >= 0.8 + area 超 → NG", v.is_ng)


def test_max_count():
    print("\n── max_count: 同类累计超数 ──")
    rules = [ClassRule(name="崩边", report_ng=True,
                       max_area=1e9, max_length=1e9, max_count=2)]
    # 3 个崩边 area 很小（max_area 不触发），但累计数超
    defects = [mk_defect("崩边", area=1, radius=0.1) for _ in range(3)]
    v = judge_per_class(defects, rules)
    check("3 个崩边 > max_count=2 → NG", v.is_ng)
    check("ng_type='崩边'", v.ng_type == "崩边")


def test_worst_picks_largest_radius():
    print("\n── 多个触发时 ng_type 取 outer_radius 最大 ──")
    rules = [
        ClassRule(name="隐裂", report_ng=True, max_area=10),
        ClassRule(name="崩边", report_ng=True, max_area=10),
    ]
    defects = [
        mk_defect("隐裂", area=100, radius=10),
        mk_defect("崩边", area=200, radius=20),     # 更"严重"
    ]
    v = judge_per_class(defects, rules)
    check("两类都触发 → ng_type=崩边（radius 大）", v.ng_type == "崩边")
    check("ng_length=20", abs(v.ng_length - 20) < 0.01)


def test_unknown_class():
    print("\n── 类别不在规则里 → 视为不报 ──")
    rules = [ClassRule(name="隐裂", report_ng=True, max_area=10)]
    v = judge_per_class([mk_defect("外星人", area=99999)], rules)
    check("未知类别 → OK", not v.is_ng)


def test_mixed_report_ng():
    print("\n── 一类 report_ng=True 一类 False，混合判定 ──")
    rules = [
        ClassRule(name="隐裂", report_ng=True,  max_area=10),
        ClassRule(name="脏污", report_ng=False, max_area=10),
    ]
    defects = [
        mk_defect("脏污", area=1000),      # 超阈值但不报
        mk_defect("隐裂", area=50),        # 超阈值且报
    ]
    v = judge_per_class(defects, rules)
    check("混合 → NG, type=隐裂", v.is_ng and v.ng_type == "隐裂")


# ============================================================
# main
# ============================================================

def main() -> int:
    print("=" * 60)
    print("per-class judge 单元测试")
    print("=" * 60)

    test_empty()
    test_report_ng_false()
    test_max_area()
    test_max_length()
    test_min_confidence()
    test_max_count()
    test_worst_picks_largest_radius()
    test_unknown_class()
    test_mixed_report_ng()

    print()
    print("=" * 60)
    if _fail_count == 0:
        print("[OK] 所有断言通过")
        return 0
    else:
        print(f"[FAIL] {_fail_count} 项失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
