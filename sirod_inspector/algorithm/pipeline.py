"""
端到端检测流水线
================
等价于 Halcon ``Run.hdev`` 中 ``Detection_process`` 主体逻辑的 Python 版：

    [uint16 原图]
        │
        │ preprocess() — 削顶 + 缩放 + 旋转 → 1024x3072 uint8
        ▼
    [预处理图]
        │
        │ Segmenter.predict() — 像素级分割得 label_map
        ▼
    [label_map]
        │
        │ 1. 保留 label∈[1,3] (Background=0 排除)
        │ 2. 排除图像顶部 _EXCLUDE_TOP_ROWS 行（伪缺陷）
        │ 3. 连通块 + 几何筛选 (outer_radius ≥ 5, area ≥ 100)
        │ 4. union + 重新连通 → 合并相邻小缺陷
        ▼
    [合并后的缺陷列表]
        │
        │ judge_by_rules() — 四阈值规则判定
        ▼
    [needs_classification?]
        │ 否 → result=OK, defect_type=""
        │ 是 ↓
        │      对每个缺陷 crop（bbox + 50 padding）
        │      → Classifier.predict() → 类别名
        │      只有 "隐裂" → result=NG, defect_type="隐裂"
        ▼
    DetectionResult

公开 API
--------
    DetectionResult                  端到端检测产出
    Pipeline(seg_path, cls_path,...) 端到端流水线
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from .inference import Classifier, Segmenter
from .judge import (
    DefectStats, JudgeConfig, JudgeVerdict, judge_by_rules,
    ClassRule, ClassJudgeVerdict, judge_per_class, DEFAULT_CLASS_RULES,
)
from .preprocess import preprocess

logger = logging.getLogger("SiRod.Pipeline")


# ============================================================
# 常量（对应 Halcon hardcode）
# ============================================================

# 排除顶部 N 行 — 对应 Halcon: gen_rectangle1(-147.17, 7.5, 93.99, 3106)
# 即 row 0..94 范围内的像素视为伪缺陷区不参与判定
_EXCLUDE_TOP_ROWS = 94

# 第一次连通块的几何筛选门槛
_MIN_OUTER_RADIUS_PX = 5        # outer_radius (最小外接圆半径)
_MIN_AREA_PX = 100              # 面积

# 缺陷分类前的 padding（bbox 四边各扩 N 像素）
# 对应 Halcon: dilation_rectangle1(50, 50) — 结构元素 50×50 参考点居中
# → 每条边各向外扩约 25 px（不是 50）
_CROP_PADDING_PX = 25

# 默认的 NG 触发类别（Halcon 端逻辑：仅"隐裂"上报 NG，其他类别记录但不报警）
# Pipeline 构造时可通过 ``ng_trigger_classes`` 参数覆盖，对应 settings.judge.ng_trigger_classes
NG_TRIGGER_CLASSES = frozenset({"隐裂"})


# ============================================================
# 输出数据结构
# ============================================================

@dataclass
class ClassifiedDefect:
    """单个缺陷的完整信息（含分类结果）"""
    bbox: tuple                       # (left, top, width, height) 在预处理图坐标系
    area: int                         # 像素面积
    outer_radius: float               # 外接圆半径（≈ 长度）
    class_name: str = ""              # 分类结果，"" 表示未做分类
    class_confidence: float = 0.0     # 分类置信度
    crop: Optional[np.ndarray] = None # 送给分类模型的小图（仅在 keep_crops=True 时填充）


@dataclass
class DetectionResult:
    """检测流水线的最终产出。

    UI / main 层会进一步把这个对象包装成 InspectData（带 rod_id / 时间戳等元数据）。
    """
    result: str                       # "OK" / "NG"
    quality: int                      # 0 / 1
    defect_type: str = ""             # NG 时为类别名（"隐裂"），OK 为 ""

    defect_count: int = 0
    max_area: float = 0.0
    sum_area: float = 0.0
    max_length: float = 0.0           # = max(outer_radius)

    processed_image: Optional[np.ndarray] = None       # 预处理后图（1024x3072 uint8）
    raw_input_image: Optional[np.ndarray] = None       # 原始输入图（uint16 15000x1024，未预处理）
    label_map: Optional[np.ndarray] = None             # seg 模型输出的像素级 label map（仅 keep_label_map=True 时填充）
    defects: List[ClassifiedDefect] = field(default_factory=list)
    seg_class_names: List[str] = field(default_factory=list)  # seg 模型的类别表（画 mask 时用）

    ct_ms: float = 0.0                # 检测耗时 ms
    judge_reasons: List[str] = field(default_factory=list)


# ============================================================
# 流水线
# ============================================================

class Pipeline:
    """端到端检测流水线：``process(image_uint16) -> DetectionResult``

    线程安全说明：本类**非线程安全**（持有 Segmenter / Classifier 句柄，
    DLL 内部缓冲被 predict 复用）。如需并发，每线程持有独立 Pipeline 实例。
    """

    def __init__(self,
                 model_seg_path: str | Path,
                 model_cls_path: str | Path,
                 judge_config: Optional[JudgeConfig] = None,
                 *,
                 ng_trigger_classes: Optional[frozenset] = None,
                 class_rules: Optional[List[ClassRule]] = None):
        """
        Parameters
        ----------
        ng_trigger_classes : frozenset[str] | None
            旧 API（向后兼容）—— 分类落在此集合中即标 NG。
            如同时传 ``class_rules``，``class_rules`` 优先。``None`` + 无 class_rules
            时用默认 ``{"隐裂"}``。
        class_rules : list[ClassRule] | None
            新 API —— 每类独立 5 字段规则。``None`` 时由 ``ng_trigger_classes``
            构造一个兼容旧行为的规则表。
        """
        self.segmenter = Segmenter(model_seg_path)
        self.classifier = Classifier(model_cls_path)
        self.judge_config = judge_config or JudgeConfig()

        # 构造 class_rules（优先用传入的，否则从 ng_trigger_classes 兼容构造）
        if class_rules is not None and class_rules:
            self.class_rules = list(class_rules)
        else:
            ng_set = (set(ng_trigger_classes) if ng_trigger_classes
                       else {"隐裂"})
            # 用默认规则模板，但把 report_ng 按 ng_set 重置
            self.class_rules = []
            for r in DEFAULT_CLASS_RULES:
                new_r = ClassRule(
                    name=r.name,
                    report_ng=(r.name in ng_set),
                    max_area=r.max_area, max_length=r.max_length,
                    max_count=r.max_count, min_confidence=r.min_confidence,
                )
                self.class_rules.append(new_r)

        # 向后兼容：暴露 ng_trigger_classes 供旧代码读
        self.ng_trigger_classes = frozenset(
            r.name for r in self.class_rules if r.report_ng
        )

        logger.info(
            f"检测流水线就绪: seg classes={self.segmenter.class_names} "
            f"cls classes={self.classifier.class_names} "
            f"judge={self.judge_config} "
            f"ng_classes={set(self.ng_trigger_classes)}"
        )

    # ─────────── 资源管理 ───────────

    def close(self) -> None:
        if getattr(self, "segmenter", None):
            self.segmenter.close()
            self.segmenter = None
        if getattr(self, "classifier", None):
            self.classifier.close()
            self.classifier = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ─────────── 主流程 ───────────

    def process(self, image_uint16: np.ndarray,
                *, keep_processed_image: bool = True,
                keep_crops: bool = False,
                keep_label_map: bool = False,
                keep_raw_input: bool = False) -> DetectionResult:
        """一根棒子从原图到判定结果的端到端处理。

        Parameters
        ----------
        image_uint16 : np.ndarray
            原图（15000 x 1024 uint16）。如果传入已预处理的 uint8 图，
            会跳过预处理直接进入推理（按 dtype 判断）。
        keep_processed_image : bool
            是否在 ``DetectionResult.processed_image`` 中返回预处理后图。
        keep_crops : bool
            是否在每个 ``ClassifiedDefect.crop`` 中保存送给分类模型的小图。
        keep_label_map : bool
            是否在 ``DetectionResult.label_map`` 中保存 seg 输出的像素级标签图。
            用于画 mask 可视化或导出训练 mask；不需要可视化时关闭省内存。

        Returns
        -------
        DetectionResult
        """
        t_start = time.perf_counter()

        # 1) 预处理（若调用方已经给了 uint8 处理图就跳过）
        if image_uint16.dtype == np.uint8:
            processed = image_uint16
        else:
            processed = preprocess(image_uint16)

        # 2) 分割推理
        seg = self.segmenter.predict(processed)
        label_map = seg.label_map

        # 3) 缺陷候选筛选 + 合并
        merged_defects = self._extract_defects(label_map, processed.shape)

        # 4) 规则判定
        verdict = judge_by_rules(merged_defects, self.judge_config)

        # 5) 构建结果对象
        result = DetectionResult(
            result="OK",
            quality=0,
            defect_type="",
            defect_count=verdict.defect_count,
            max_area=verdict.max_area,
            sum_area=verdict.sum_area,
            max_length=verdict.max_length,
            processed_image=processed if keep_processed_image else None,
            raw_input_image=(image_uint16 if keep_raw_input
                              and image_uint16.dtype != np.uint8 else None),
            label_map=label_map.copy() if keep_label_map else None,
            seg_class_names=list(seg.class_names) if keep_label_map else [],
            defects=[ClassifiedDefect(
                bbox=d.bbox, area=d.area, outer_radius=d.outer_radius,
            ) for d in merged_defects],
            judge_reasons=list(verdict.reasons),
        )

        # 6) 分类 + per-class 判定
        #    历史上只在 verdict.needs_classification（全局阈值 max_area /
        #    sum_area / max_count / max_length 超限）时才分类。但 iter8 上
        #    了 per-class 规则后，每类可能有更严的阈值（如 "隐裂 max_area=50"
        #    远低于全局 max_area=500）。如果继续按全局门控，per-class 永远
        #    没机会跑 → 真正的小型 NG 缺陷被吞。
        #    修复：只要有候选缺陷就分类，让 per-class 规则有机会判 NG。
        if result.defects:
            self._classify_defects(processed, result, keep_crops=keep_crops)

        result.ct_ms = (time.perf_counter() - t_start) * 1000.0
        logger.info(
            f"检测完成: result={result.result} type={result.defect_type or '-'} "
            f"count={result.defect_count} max_area={result.max_area:.1f} "
            f"sum_area={result.sum_area:.1f} max_length={result.max_length:.2f} "
            f"ct={result.ct_ms:.0f}ms"
        )
        return result

    # ─────────── 内部步骤 ───────────

    def _extract_defects(self, label_map: np.ndarray,
                          image_shape: tuple) -> List[DefectStats]:
        """从分割 label_map 提取合并后的缺陷统计列表。

        对应 Halcon::

            threshold(outimage, Regions, 1, 3)
            difference(Regions, RegionDifference1, Regions)   # 排除顶部
            connection(Regions, ConnectedRegions)
            select_shape(... 'outer_radius' >= 5 ...)
            select_shape(... 'area' >= 100 ...)
            union1(...) + connection(...)                     # 合并相邻
            region_features(... 'area', 'outer_radius')
        """
        rows_img, cols_img = image_shape[:2]
        rows_lbl, cols_lbl = label_map.shape

        # label_map 尺寸可能与输入图不同（取决于模型输出分辨率）
        # 缺陷坐标全部映射到输入图坐标系
        scale_x = cols_img / cols_lbl
        scale_y = rows_img / rows_lbl

        # 1) 保留 label ∈ [1, 3]
        defect_mask = ((label_map >= 1) & (label_map <= 3)).astype(np.uint8)

        # 2) 排除顶部 N 行（按 label_map 自身分辨率计算）
        exclude_rows_lbl = int(round(_EXCLUDE_TOP_ROWS / scale_y))
        if exclude_rows_lbl > 0:
            defect_mask[:exclude_rows_lbl, :] = 0

        if not defect_mask.any():
            return []

        # 3) 第一次 cc + 几何筛选
        num1, labels1, stats1, _ = cv2.connectedComponentsWithStats(
            defect_mask, connectivity=8,
        )
        keep_mask = np.zeros_like(defect_mask)
        for lbl in range(1, num1):
            x, y, w, h, area = stats1[lbl]
            outer_radius_lbl = self._estimate_outer_radius(labels1, lbl)
            # outer_radius 阈值的语义是「在输入图坐标系」, 这里 label_map 可能被缩放过
            # 按 max(scale_x, scale_y) 折算到输入图坐标
            outer_radius_img = outer_radius_lbl * max(scale_x, scale_y)
            area_img = area * scale_x * scale_y

            if outer_radius_img < _MIN_OUTER_RADIUS_PX:
                continue
            if area_img < _MIN_AREA_PX:
                continue
            keep_mask[labels1 == lbl] = 1

        if not keep_mask.any():
            return []

        # 4) union + 第二次 cc（合并相邻缺陷）
        # Halcon union1 + connection 等价于在合并 mask 上重新做 cc。
        # 这里相邻缺陷会因为之前的 connectivity=8 已经各自连通，
        # 二次 cc 主要起到「重新分组」的作用 — 与 Halcon 保持一致。
        num2, labels2, stats2, _ = cv2.connectedComponentsWithStats(
            keep_mask, connectivity=8,
        )

        defects: List[DefectStats] = []
        for lbl in range(1, num2):
            x, y, w, h, area = stats2[lbl]
            outer_radius_lbl = self._estimate_outer_radius(labels2, lbl)

            # 坐标 / 几何量映射到输入图坐标系
            bbox_img = (
                int(round(x * scale_x)),
                int(round(y * scale_y)),
                int(round(w * scale_x)),
                int(round(h * scale_y)),
            )
            area_img = int(round(area * scale_x * scale_y))
            outer_radius_img = float(outer_radius_lbl * max(scale_x, scale_y))

            defects.append(DefectStats(
                area=area_img,
                outer_radius=outer_radius_img,
                bbox=bbox_img,
            ))
        return defects

    @staticmethod
    def _estimate_outer_radius(label_map: np.ndarray, label: int) -> float:
        """估算给定连通块的最小外接圆半径（Halcon ``outer_radius``）。

        通过 ``cv2.minEnclosingCircle`` 对该 label 的轮廓点求最小外接圆。
        若 contour 为空（理论上不应出现），返回 bbox 对角线 / 2。
        """
        mask = (label_map == label).astype(np.uint8)
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE,
        )
        if contours:
            # 多个外轮廓时取面积最大的那个
            largest = max(contours, key=cv2.contourArea)
            (_cx, _cy), radius = cv2.minEnclosingCircle(largest)
            return float(radius)
        # 兜底：用 bbox 对角线 / 2
        ys, xs = np.where(mask > 0)
        if ys.size == 0:
            return 0.0
        return 0.5 * float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min()))

    @staticmethod
    def _square_crop_bbox(left: int, top: int, w: int, h: int,
                           padding: int, img_w: int, img_h: int) -> tuple:
        """计算「长边为边长、缺陷居中、不出图」的正方形 crop bbox。

        步骤：
          1) bbox 四边各扩 padding 像素
          2) 以扩张后长边为边长，构造以缺陷中心为中心的正方形
          3) 若越界，整体向内平移以保持正方形大小
          4) 兜底：若正方形比图本身还大，再 clamp 到图像边界
        返回 ``(x0, y0, x1, y1)`` 半开区间，适用于 numpy 切片。
        """
        pw = w + 2 * padding
        ph = h + 2 * padding
        side = max(pw, ph)
        cx = left + w / 2.0
        cy = top + h / 2.0
        x0 = int(round(cx - side / 2.0))
        y0 = int(round(cy - side / 2.0))
        x1 = x0 + side
        y1 = y0 + side
        # 越界往内平移，保持正方形大小
        if x0 < 0:
            x1 -= x0
            x0 = 0
        if y0 < 0:
            y1 -= y0
            y0 = 0
        if x1 > img_w:
            x0 -= (x1 - img_w)
            x1 = img_w
        if y1 > img_h:
            y0 -= (y1 - img_h)
            y1 = img_h
        # 兜底：正方形比整图还大时再 clamp
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(img_w, x1)
        y1 = min(img_h, y1)
        return x0, y0, x1, y1

    def _classify_defects(self, image: np.ndarray,
                           result: DetectionResult,
                           *, keep_crops: bool = False) -> None:
        """对每个缺陷区域 crop → 分类 → 按 per-class 规则判 NG。

        分类完成后调用 ``judge_per_class``，按每类独立的 5 字段规则
        （report_ng/max_area/max_length/max_count/min_confidence）做最终判定。
        """
        rows, cols = image.shape[:2]

        for i, classified in enumerate(result.defects):
            x, y, w, h = classified.bbox

            # 长边为边长的正方形 crop，缺陷居中、不出图
            x0, y0, x1, y1 = self._square_crop_bbox(
                x, y, w, h, _CROP_PADDING_PX, cols, rows,
            )
            if x1 <= x0 or y1 <= y0:
                continue

            crop = image[y0:y1, x0:x1]
            if crop.size == 0:
                continue

            try:
                cls = self.classifier.predict(crop)
            except Exception as e:
                logger.warning(f"缺陷 #{i} 分类失败: {e}")
                continue

            classified.class_name = cls.name
            classified.class_confidence = float(cls.confidence)
            if keep_crops:
                # copy 一份避免外部修改预处理图时连带改动
                classified.crop = crop.copy()

        # ── 按 per-class 规则做最终 NG 判定 ──
        verdict = judge_per_class(result.defects, self.class_rules)
        if verdict.is_ng:
            result.result = "NG"
            result.quality = 1
            result.defect_type = verdict.ng_type
            result.max_length = float(verdict.ng_length)
            # 把 per-class reasons 合并到 judge_reasons，便于在 UI / 日志看
            result.judge_reasons = list(result.judge_reasons) + [
                f"[per-class] {r}" for r in verdict.reasons
            ]
