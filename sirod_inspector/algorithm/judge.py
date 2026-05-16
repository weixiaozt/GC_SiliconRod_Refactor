"""
缺陷判定规则
============
对应 Halcon Detection_process 的判定环节：

  1. 先按四个阈值判定是否进入「NG 候选」分支
        max_area      —— 单个缺陷面积上限
        sum_area      —— 总面积上限
        max_length    —— 单个缺陷 outer_radius（外接圆半径）上限
        max_count     —— 缺陷个数上限
     任一超限即进入 NG 候选。

  2. 进入 NG 候选后，对每个缺陷送分类模型；只有 ``classification_name == "隐裂"``
     才标 NG (quality=1)。其它分类（崩边 / 缺口 / 其它）记录但不报警。
     ↑ 这部分在 pipeline.py 中实现，本模块只负责第 1 步的纯规则判定。

公开 API
--------
    JudgeConfig    — 4 个阈值参数
    DefectStats    — 单个候选缺陷的统计量
    JudgeVerdict   — 第 1 步规则判定的输出
    judge_by_rules — 执行第 1 步规则判定
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


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
