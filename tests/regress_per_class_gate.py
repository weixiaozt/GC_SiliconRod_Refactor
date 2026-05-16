"""
Pipeline 调用 _classify_defects 的门控测试
==============================================
iter12 修复：之前 ``if verdict.needs_classification`` 全局门控会让
"小型 NG 缺陷"（per-class max_area 比全局严）被吞，永远不进 cls。

修复后：``if result.defects`` — 只要有候选缺陷就分类，让 per-class
规则有机会判 NG。零候选场景仍然短路（不进 cls）。

本测试不依赖真实模型 — 用 Mock 替换 Segmenter + Classifier，验证
分类被调用 / 没被调用 的次数符合预期。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

import numpy as np


_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  {'[OK ]' if cond else '[FAIL]'} {name}"
           + (f"  — {detail}" if detail else ""))
    if not cond:
        _fail += 1


def main() -> int:
    print("=" * 60)
    print("Pipeline._classify_defects 门控（iter12 修复）")
    print("=" * 60)

    from sirod_inspector.algorithm.pipeline import Pipeline
    from sirod_inspector.algorithm.judge import JudgeConfig, ClassRule

    # 用宽松全局阈值（max_area=1e9 等价不限） + 严格 per-class
    # （这样小型缺陷不触发全局门控，必须 per-class 才能判 NG）
    judge_cfg = JudgeConfig(
        max_area=1e9, sum_area=1e9, max_length=1e9, max_count=1_000_000)
    rules = [
        ClassRule(name="隐裂", report_ng=True,
                  max_area=10.0, max_length=2.0,
                  max_count=10, min_confidence=0.0),
        ClassRule(name="其他", report_ng=False,
                  max_area=1e9, max_length=1e9,
                  max_count=1_000_000, min_confidence=0.0),
    ]

    # 不真的加载 dnninfer.dll — 直接 patch Segmenter / Classifier 构造
    with patch("sirod_inspector.algorithm.pipeline.Segmenter") as MS, \
         patch("sirod_inspector.algorithm.pipeline.Classifier") as MC:
        MS.return_value = MagicMock()
        MC.return_value = MagicMock()
        p = Pipeline("fake_seg.m", "fake_cls.m",
                     judge_cfg, class_rules=rules)

    # 场景 1：零缺陷 → 不调 cls
    p.classifier.predict.reset_mock()
    seg_out = MagicMock()
    seg_out.label_map = np.zeros((1024, 3072), dtype=np.uint8)
    seg_out.class_names = ["bg"]
    p.segmenter.predict.return_value = seg_out
    # 调 process 走完整流程
    img = np.zeros((1024, 3072), dtype=np.uint8)
    result = p.process(img)
    check("零候选 → cls.predict 未调用",
          p.classifier.predict.call_count == 0,
          f"call_count={p.classifier.predict.call_count}")
    check("零候选 → result=OK", result.result == "OK")

    # 场景 2：有候选（即使小） → cls 必被调
    p.classifier.predict.reset_mock()
    lm = np.zeros((1024, 3072), dtype=np.uint8)
    # 在中央位置画一个 20×20 的小缺陷区域（label=1）
    lm[500:520, 1500:1520] = 1
    seg_out2 = MagicMock()
    seg_out2.label_map = lm
    seg_out2.class_names = ["bg", "defect"]
    p.segmenter.predict.return_value = seg_out2
    # mock classifier 返回 "其他"
    cls_out = MagicMock()
    cls_out.name = "其他"
    cls_out.confidence = 0.95
    p.classifier.predict.return_value = cls_out

    result2 = p.process(img)
    check("小候选缺陷 → cls.predict 被调",
          p.classifier.predict.call_count >= 1,
          f"call_count={p.classifier.predict.call_count}")
    check("小候选 + 其他类 → result=OK",
          result2.result == "OK")
    check("defect 的 class_name 已写回",
          len(result2.defects) > 0 and result2.defects[0].class_name == "其他")

    # 场景 3：缺陷被分类为 "隐裂" → 触发 per-class NG（即使全局未超）
    p.classifier.predict.reset_mock()
    cls_out2 = MagicMock()
    cls_out2.name = "隐裂"
    cls_out2.confidence = 0.95
    p.classifier.predict.return_value = cls_out2

    result3 = p.process(img)
    check("隐裂 + area=400 (20×20)，per-class max_area=10 → NG",
          result3.result == "NG",
          f"result={result3.result}, defect_type={result3.defect_type}")
    check("ng_type=隐裂",
          result3.defect_type == "隐裂",
          f"defect_type={result3.defect_type}")

    print()
    if _fail == 0:
        print("[OK] per-class 门控正确（小缺陷也走 cls）")
        return 0
    print(f"[FAIL] {_fail} 个失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
