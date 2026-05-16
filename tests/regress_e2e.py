"""
端到端回归测试（带断言，固化历史行为）
=========================================
**目的**：每次代码改动后跑一遍，确保以下已知行为不被破坏：

| 测试样本 | 预期 result | 预期 defect_type | 备注 |
|---|---|---|---|
| test_image/0561.bmp | OK   | ""    | seg 检出但 cls=「其他」 → 不触发 NG |
| test_image/0581.bmp | NG   | 隐裂  | 2 个候选，其中 1 个 cls=「隐裂」 |
| test_image/1dab*.bmp | NG  | 隐裂  | 修了 padding bug 后从 OK 转 NG 的边界 case |
| test_image/368.bmp  | OK   | ""    | seg 检出但 cls=「其他」 |

每个样本都覆盖：
  1. 图像 → Pipeline.process → DetectionResult
  2. DetectionResult → detection_to_inspect_data → InspectData
  3. InspectData 字段断言（result / defect_type / image / 关键字段非空）

任何一条 fail 都意味着行为回归 — 需查最近 commit。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imread_safe
setup_console_utf8()

import cv2
import numpy as np

from sirod_inspector.algorithm import Pipeline, JudgeConfig
from sirod_inspector.core.inspect_engine import detection_to_inspect_data


# ============================================================
# 期望值表（按 iter4 + iter5 已固化的行为）
# ============================================================

# 这组期望值反映 iter7+ 行为（正方形 crop + per-class judge + 默认仅"隐裂"报NG）。
# 矩形 crop 时期 0581/1dab 会被分类为「隐裂」→ NG，但用户在 iter7 选择保留
# 正方形 crop（"以后训练用正方形"），分类结果不同，这两张退到 OK。
# 注：分类 confidence 不是稳定值，只断言类别，不断言精确 conf
EXPECTED = [
    {
        "file":        "0561.bmp",
        "result":      "OK",
        "defect_type": "",
        "min_count":   1,
        "expected_classes": {"其他"},
        "min_max_area": 1000,
    },
    {
        "file":        "0581.bmp",
        # 正方形 crop 下分类结果是「脏污」+「其他」，两类都非 NG 触发 → OK
        "result":      "OK",
        "defect_type": "",
        "min_count":   2,
        "expected_classes": {"其他", "脏污"},
        "min_max_area": 1000,
    },
    {
        "file":        "1dab2cdb475e29ac9e8ab2506c0fc7ef.bmp",
        # 正方形 crop 下分类为「OK」，置信度 ~0.60；不报 NG
        "result":      "OK",
        "defect_type": "",
        "min_count":   1,
        "expected_classes": {"OK"},
        "min_max_area": 8000,
    },
    {
        "file":        "368.bmp",
        "result":      "OK",
        "defect_type": "",
        "min_count":   1,
        "expected_classes": {"其他"},
        "min_max_area": 4000,
    },
]


# ============================================================
# 测试运行
# ============================================================

class AssertionFailure(Exception):
    """断言失败"""


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionFailure(msg)


def run_one(pipe: Pipeline, exp: dict) -> tuple[bool, str]:
    """跑单个样本，返回 (pass, 详情)"""
    img_path = _REPO_ROOT / "test_image" / exp["file"]
    if not img_path.is_file():
        return False, f"图片不存在: {img_path}"

    img = imread_safe(img_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        return False, f"读图失败: {img_path}"

    t0 = time.perf_counter()
    result = pipe.process(img, keep_processed_image=True)
    ct_ms = (time.perf_counter() - t0) * 1000

    # 装配 InspectData
    data = detection_to_inspect_data(
        result, rod_id=f"TEST_{exp['file']}", inspect_id=999,
    )

    # ── 断言 ──
    try:
        # 1. result
        expect(data.result == exp["result"],
               f"result: 期望 {exp['result']!r}, 实际 {data.result!r}")

        # 2. defect_type
        expect(data.defect_type == exp["defect_type"],
               f"defect_type: 期望 {exp['defect_type']!r}, 实际 {data.defect_type!r}")

        # 3. 缺陷数
        expect(data.defect_count >= exp["min_count"],
               f"defect_count: 期望 ≥ {exp['min_count']}, 实际 {data.defect_count}")

        # 4. 最大面积
        expect(data.max_area >= exp["min_max_area"],
               f"max_area: 期望 ≥ {exp['min_max_area']}, 实际 {data.max_area:.0f}")

        # 5. 分类类别落在期望集合
        seen_classes = {
            d.get("class_name") for d in data.raw_json.get("defects", [])
            if d.get("class_name")
        }
        # 检查至少有一个类别落在 expected_classes 里
        intersect = seen_classes & exp["expected_classes"]
        expect(bool(intersect),
               f"分类类别: 期望 ∩ {exp['expected_classes']} 非空, "
               f"实际收到 {seen_classes}")

        # 6. InspectData 必填字段非空
        expect(data.image is not None and data.image.size > 0,
               f"image 为空")
        expect(data.rod_id, "rod_id 为空")
        expect(data.timestamp, "timestamp 为空")
        expect(data.ct > 0, "ct 应 > 0")

        return True, (f"OK  result={data.result}  type={data.defect_type or '-'}  "
                      f"count={data.defect_count}  max_area={data.max_area:.0f}  "
                      f"ct={ct_ms:.0f}ms")
    except AssertionFailure as e:
        return False, f"FAIL  {e}  [实际: result={data.result}, type={data.defect_type}, count={data.defect_count}, max_area={data.max_area:.0f}]"


def main() -> int:
    print("=" * 60)
    print("端到端回归测试")
    print("=" * 60)

    seg = _REPO_ROOT / "models" / "Model_seg.m"
    cls = _REPO_ROOT / "models" / "Model_cls.m"
    if not seg.is_file() or not cls.is_file():
        print(f"[SKIP] 模型不存在: {seg} / {cls}")
        return 2

    pass_count = 0
    fail_count = 0
    fails: list[tuple] = []

    with Pipeline(str(seg), str(cls), JudgeConfig()) as pipe:
        for exp in EXPECTED:
            print(f"\n─── {exp['file']} ──────")
            print(f"  期望: result={exp['result']}, type={exp['defect_type'] or '-'}, "
                  f"count≥{exp['min_count']}, max_area≥{exp['min_max_area']}")
            ok, detail = run_one(pipe, exp)
            print(f"  → {detail}")
            if ok:
                pass_count += 1
            else:
                fail_count += 1
                fails.append((exp["file"], detail))

    print()
    print("=" * 60)
    print(f"汇总: ✓ {pass_count} 通过   ✗ {fail_count} 失败")
    if fails:
        print("\n失败明细：")
        for fname, detail in fails:
            print(f"  · {fname}: {detail}")
        print()
        print("如最近改了 algorithm/ 或 core/inspect_engine.py，请检查 diff。"
              "可用 git bisect 定位回归 commit。")
        return 1
    print("\n[OK] 所有已知样本行为与基线一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
