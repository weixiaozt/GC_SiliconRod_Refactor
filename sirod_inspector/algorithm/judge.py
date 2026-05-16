"""
缺陷判定规则
============
分两阶段：

  阶段 1（``judge_by_rules``）：按 4 个全局阈值判定是否进入分类。
       任一超限则进入 NG 候选 → 送分类模型。

  阶段 2（``judge_per_class``）：分类完成后，按每个类别独立的规则做最终 NG 判定。
       每个类别有 5 个字段：
         - report_ng        是否计入 NG（不计入则该类即使超阈值也不报）
         - max_area         单个缺陷面积上限（超即 NG）
         - max_length       单个缺陷 outer_radius 上限（超即 NG）
         - max_count        该类缺陷个数上限（一根棒上同类超 N 个即 NG）
         - min_confidence   分类置信度阈值（低于此值视为模型没把握，不报 NG）

公开 API
--------
    JudgeConfig          — 全局几何阈值（阶段 1）
    ClassRule            — 单个类别的判定规则（阶段 2）
    DefectStats          — 候选缺陷的几何统计量
    JudgeVerdict         — 阶段 1 输出
    ClassJudgeVerdict    — 阶段 2 输出
    judge_by_rules       — 阶段 1 判定
    judge_per_class      — 阶段 2 判定
    DEFAULT_NG_CLASSES   — 默认 NG 类别（沿用 Halcon "隐裂" 行为）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ============================================================
# 数据结构
# ============================================================

@dataclass(frozen=True)
class JudgeConfig:
    """缺陷判定阈值。

    任一指标超阈值即视为「NG 候选」需要进一步分类确认。

    默认值与 Halcon LoadParam 一致：
        max_area     = 10    (像素)
        sum_area     = 10    (像素)
        max_count    = 10    (个数)
        max_length   = 2     (像素，outer_radius)
    """
    max_area:   float = 10.0     # 单个缺陷面积上限
    sum_area:   float = 10.0     # 总面积上限
    max_count:  int = 10         # 缺陷个数上限
    max_length: float = 2.0      # 单个缺陷 outer_radius 上限


# ============================================================
# 阶段 2：按类别独立规则
# ============================================================

@dataclass
class ClassRule:
    """单个缺陷类别的判定规则。

    所有字段都是「触发 NG 的上限」语义 — 超过即 NG，等于不触发。
    """
    name: str = ""                  # 类别名（"隐裂"/"崩边"/...）
    report_ng: bool = False         # 是否计入 NG（False 时该类即使超阈值也不报）
    max_area: float = 1e9           # 单个缺陷面积上限（超即 NG）
    max_length: float = 1e9         # 单个缺陷 outer_radius 上限
    max_count: int = 1_000_000      # 该类缺陷个数上限（同一根棒上同类超此值即 NG）
    min_confidence: float = 0.0     # 分类置信度下限（低于此值视为模型没把握，不报 NG）


# 沿用 Halcon 行为的默认规则：只把 "隐裂" 算 NG，其它类全部放行
def _make_default_class_rules() -> List[ClassRule]:
    return [
        ClassRule(name="隐裂",
                  report_ng=True,
                  max_area=10.0, max_length=2.0,
                  max_count=10, min_confidence=0.0),
        ClassRule(name="崩边", report_ng=False),
        ClassRule(name="其他", report_ng=False),
        ClassRule(name="脏污", report_ng=False),
        ClassRule(name="线痕", report_ng=False),
        ClassRule(name="拼缝", report_ng=False),
        ClassRule(name="OK",   report_ng=False),
        ClassRule(name="缺口", report_ng=False),
    ]


DEFAULT_CLASS_RULES = _make_default_class_rules()


@dataclass
class ClassJudgeVerdict:
    """阶段 2 输出：综合所有缺陷的最终 NG 判定"""
    is_ng: bool = False
    ng_type: str = ""               # 最严重 NG 类别的名字（按 outer_radius 最大者）
    ng_length: float = 0.0          # 最严重 NG 的 outer_radius
    reasons: List[str] = field(default_factory=list)


def judge_per_class(classified_defects: list,
                     rules: List[ClassRule]) -> ClassJudgeVerdict:
    """阶段 2：根据每个 ClassRule 检查所有已分类缺陷，得出最终 NG 判定。

    Parameters
    ----------
    classified_defects : list[ClassifiedDefect]
        来自 pipeline 的已分类缺陷（含 area / outer_radius / class_name / class_confidence）。
    rules : list[ClassRule]
        每类的判定规则。

    Returns
    -------
    ClassJudgeVerdict
        ``is_ng=True`` 当至少一个缺陷触发任一规则；
        ``ng_type`` 取最严重 NG（outer_radius 最大）的类别。
    """
    # 类别名 → 规则
    rule_map: Dict[str, ClassRule] = {r.name: r for r in rules}

    # 统计每类计数（用于 max_count 判定）
    per_class_count: Dict[str, int] = {}
    for d in classified_defects:
        nm = d.class_name or ""
        if not nm:
            continue
        per_class_count[nm] = per_class_count.get(nm, 0) + 1

    reasons: List[str] = []
    worst_length = -1.0
    worst_type = ""

    # max_count 类别级判定
    for cls_name, cnt in per_class_count.items():
        rule = rule_map.get(cls_name)
        if not rule or not rule.report_ng:
            continue
        if cnt > rule.max_count:
            reasons.append(
                f"{cls_name} 数量={cnt} > {rule.max_count}"
            )
            # 找该类置信度最高的缺陷作为代表
            for d in classified_defects:
                if d.class_name == cls_name and d.outer_radius > worst_length:
                    worst_length = d.outer_radius
                    worst_type = cls_name

    # 单缺陷级判定
    for d in classified_defects:
        nm = d.class_name or ""
        rule = rule_map.get(nm)
        if not rule or not rule.report_ng:
            continue
        if d.class_confidence < rule.min_confidence:
            continue   # 模型没把握，不报
        triggered = False
        if d.area > rule.max_area:
            reasons.append(
                f"{nm} area={d.area} > {rule.max_area:.0f}"
            )
            triggered = True
        if d.outer_radius > rule.max_length:
            reasons.append(
                f"{nm} length={d.outer_radius:.1f} > {rule.max_length:.1f}"
            )
            triggered = True
        if triggered and d.outer_radius > worst_length:
            worst_length = d.outer_radius
            worst_type = nm

    return ClassJudgeVerdict(
        is_ng=bool(reasons),
        ng_type=worst_type,
        ng_length=max(0.0, worst_length),
        reasons=reasons,
    )


@dataclass
class DefectStats:
    """单个候选缺陷区域的几何统计量。

    对应 Halcon 端 ``region_features`` 提取的特征：
        area          — 像素面积
        outer_radius  — 最小外接圆半径
        bbox          — (left, top, width, height) 轴对齐外接矩形
    """
    area: int
    outer_radius: float
    bbox: tuple  # (left, top, width, height)


@dataclass
class JudgeVerdict:
    """规则判定结果。

    Attributes
    ----------
    needs_classification : bool
        是否触发 NG 候选（需要进入分类阶段做二次确认）。
        ``False`` 表示规则上已通过，无需分类。
    defect_count : int
        缺陷个数（Halcon 中 ``|AreaValue|-1`` 的等价 — 详见 Notes）。
    max_area : float
        最大单缺陷面积。
    sum_area : float
        总面积。
    max_length : float
        最大 outer_radius。
    reasons : list[str]
        触发 NG 候选的具体规则（用于日志和调试）。

    Notes
    -----
    Halcon 端 ``|AreaValue|-1`` 是 Halcon tuple 长度的奇怪用法，
    可理解为「缺陷数 - 1」。这里我们就用「真实缺陷数」语义，
    并对应改为 ``defect_count > max_count`` 判定。
    """
    needs_classification: bool
    defect_count: int
    max_area: float
    sum_area: float
    max_length: float
    reasons: List[str]


# ============================================================
# 规则判定
# ============================================================

def judge_by_rules(defects: List[DefectStats],
                    config: JudgeConfig) -> JudgeVerdict:
    """规则判定：根据缺陷统计量决定是否进入分类阶段。

    任一指标超过 ``config`` 中的阈值，就视为「NG 候选」，
    返回 ``needs_classification=True``。否则返回 False（直接 OK）。

    Parameters
    ----------
    defects : list[DefectStats]
        合并后的缺陷区域列表（已经过几何筛选）。
    config : JudgeConfig
        阈值配置。

    Returns
    -------
    JudgeVerdict
    """
    count = len(defects)
    if count == 0:
        return JudgeVerdict(
            needs_classification=False,
            defect_count=0,
            max_area=0.0, sum_area=0.0, max_length=0.0,
            reasons=[],
        )

    areas = [d.area for d in defects]
    radii = [d.outer_radius for d in defects]
    max_area = float(max(areas))
    sum_area = float(sum(areas))
    max_length = float(max(radii))

    reasons: List[str] = []
    if max_area > config.max_area:
        reasons.append(
            f"max_area={max_area:.1f}>thr({config.max_area:.1f})"
        )
    if sum_area > config.sum_area:
        reasons.append(
            f"sum_area={sum_area:.1f}>thr({config.sum_area:.1f})"
        )
    if max_length > config.max_length:
        reasons.append(
            f"max_length={max_length:.2f}>thr({config.max_length:.2f})"
        )
    if count > config.max_count:
        reasons.append(
            f"count={count}>thr({config.max_count})"
        )

    return JudgeVerdict(
        needs_classification=bool(reasons),
        defect_count=count,
        max_area=max_area,
        sum_area=sum_area,
        max_length=max_length,
        reasons=reasons,
    )
