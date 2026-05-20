"""
算法层：图像预处理 + 深度学习推理 + 缺陷判定 + 端到端流水线
==========================================================
- inference.py   深度学习推理 DLL 的 Python 封装
- preprocess.py  原图预处理（消除明暗行 + 旋转 + 缩到模型尺寸）
- judge.py       面积/个数/长度阈值规则判定
- pipeline.py    端到端流水线 preprocess → seg → cc → crop → cls → judge
"""

from .inference import (
    Classifier,
    Segmenter,
    ClsResult,
    SegResult,
    SegRect,
    INFER_MODE_CPU,
    INFER_MODE_GPU,
    init_runtime,
)
from .preprocess import (
    preprocess,
    preprocess_with_debug,
    compute_clip_threshold,
)
from .judge import (
    JudgeConfig,
    DefectStats,
    JudgeVerdict,
    judge_by_rules,
    ClassRule,
    ClassJudgeVerdict,
    judge_per_class,
    DEFAULT_CLASS_RULES,
)
from .pipeline import (
    Pipeline,
    DetectionResult,
    ClassifiedDefect,
    NG_TRIGGER_CLASSES,
)
from .overlay import (
    draw_marked_full,
    draw_marked_crop,
    color_for_label,
)
from .units import (
    radius_px_to_length_mm,
    area_px_to_mm2,
)

__all__ = [
    # inference
    "Classifier", "Segmenter", "ClsResult", "SegResult", "SegRect",
    "INFER_MODE_CPU", "INFER_MODE_GPU", "init_runtime",
    # preprocess
    "preprocess", "preprocess_with_debug", "compute_clip_threshold",
    # judge
    "JudgeConfig", "DefectStats", "JudgeVerdict", "judge_by_rules",
    "ClassRule", "ClassJudgeVerdict", "judge_per_class", "DEFAULT_CLASS_RULES",
    # pipeline
    "Pipeline", "DetectionResult", "ClassifiedDefect", "NG_TRIGGER_CLASSES",
    # overlay
    "draw_marked_full", "draw_marked_crop", "color_for_label",
    # units
    "radius_px_to_length_mm", "area_px_to_mm2",
]
