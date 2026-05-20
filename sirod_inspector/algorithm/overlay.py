"""
缺陷可视化叠加层
================
在干净图上画 mask + bbox + 类别标签，用于给客户看。

两个核心函数::

    draw_marked_full(image, label_map, defects, class_names)
        → 在完整大图上叠 mask（半透明）+ 画所有缺陷外接框 + 写分类

    draw_marked_crop(crop, defect, bbox_in_crop=None)
        → 在缺陷小图上画 mask 区 + 标分类名

输入图都是 uint8 灰度（preprocess 后），输出 BGR 3 通道 uint8。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Optional, Union

import cv2
import numpy as np


# ============================================================
# 配色（按类别固定，同类别每次同色）
# ============================================================

def _stable_label_int(label: Union[int, str]) -> int:
    """把 label（int 或 str）映射成稳定的整数种子。

    str 走 md5，跨 Python 进程稳定（``hash(str)`` 在 Python 3.3+ 默认
    每次启动重随机化，会让"同一类别每次不同色"）。
    """
    if isinstance(label, str):
        return int.from_bytes(
            hashlib.md5(label.encode("utf-8")).digest()[:4], "big"
        )
    return int(label)


def color_for_label(label: Union[int, str]) -> tuple:
    """给 label（int index 或 str 类别名）一个固定但区分度高的 BGR 颜色。

    同一 label 每次返回同色（跨 Python 进程稳定）。
    """
    seed = _stable_label_int(label)
    rng = np.random.default_rng(seed=seed * 9973 + 1)
    bgr = rng.integers(64, 256, size=3, dtype=np.int32)
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


# ============================================================
# 中文文字渲染（cv2.putText 不支持中文，用 PIL）
# ============================================================

_PIL_FONT_CACHE: dict = {}


def _get_font(size: int):
    from PIL import ImageFont
    if size in _PIL_FONT_CACHE:
        return _PIL_FONT_CACHE[size]
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for p in candidates:
        if Path(p).is_file():
            font = ImageFont.truetype(p, size)
            _PIL_FONT_CACHE[size] = font
            return font
    font = ImageFont.load_default()
    _PIL_FONT_CACHE[size] = font
    return font


def _put_text(img_bgr: np.ndarray, text: str, xy: tuple,
               font_size: int = 18,
               color: tuple = (255, 255, 255),
               bg: Optional[tuple] = None,
               pad: int = 3) -> np.ndarray:
    """在 BGR 图上写一条支持中文的文字（单条；多条用 ``_put_texts_batch``）"""
    return _put_texts_batch(
        img_bgr, [(xy, text, color, bg)], font_size=font_size, pad=pad
    )


def _put_texts_batch(img_bgr: np.ndarray,
                      items: list,
                      *,
                      font_size: int = 18,
                      pad: int = 3) -> np.ndarray:
    """一次 PIL pass 画多条文字。

    items: list of ``((x, y), text, color_bgr, bg_bgr_or_None)``。
    比循环调用 ``_put_text`` 快 N 倍（避免 N 次 BGR↔RGB 抖动）。
    """
    if not items:
        return img_bgr
    from PIL import Image, ImageDraw
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    font = _get_font(font_size)
    W, H = pil.size
    for xy, text, color, bg in items:
        x, y = xy
        # 夹紧到图像内：贴右/上边缘的缺陷，标签往左/下挪，避免跑出画面被截断
        tb = draw.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        x = min(max(0, x), max(0, W - tw - pad - 1))
        y = min(max(0, y), max(0, H - th - pad - 1))
        bbox = draw.textbbox((x, y), text, font=font)
        if bg is not None:
            draw.rectangle(
                (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad),
                fill=(bg[2], bg[1], bg[0]),
            )
        draw.text((x, y), text,
                  fill=(color[2], color[1], color[0]), font=font)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


# ============================================================
# 主接口
# ============================================================

def to_bgr(img: np.ndarray) -> np.ndarray:
    """灰度图转 BGR 3 通道；已经是 BGR 则原样返回 copy"""
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img.copy()


def draw_marked_full(image_gray: np.ndarray,
                      label_map: Optional[np.ndarray],
                      defects: Iterable,
                      seg_class_names: list = None,
                      ng_trigger_classes: set = None,
                      *,
                      mask_alpha: float = 0.4) -> np.ndarray:
    """在完整大图上叠 mask + 画外接框 + 类别标签。

    Parameters
    ----------
    image_gray : np.ndarray
        预处理后大图 (H, W) uint8 灰度。
    label_map : np.ndarray | None
        seg 输出的像素 label 图。可能尺寸与 image 不同，自动 resize。
        ``None`` 时跳过 mask 叠加，只画外接框。
    defects : Iterable[ClassifiedDefect]
        缺陷列表（来自 DetectionResult.defects）。
    seg_class_names : list[str]
        seg 类别名表（用于在 mask 上加文字 — 当前未启用，留作扩展）。
    ng_trigger_classes : set[str] | None
        触发 NG 的分类集合，用于把这些缺陷的框画红色，其余画黄色。
    mask_alpha : float
        mask 透明度（0=不显示，1=完全覆盖原图）。
    """
    if ng_trigger_classes is None:
        ng_trigger_classes = {"隐裂"}

    vis = to_bgr(image_gray)
    rows, cols = image_gray.shape[:2]

    # 1) mask 染色已禁用（按现场要求：只要框 + 类别名，不要半透明色块）
    #    保留 label_map 参数兼容旧调用方，但不再渲染 mask 覆盖。
    #    如需恢复，把下面 _DRAW_MASK 改 True。
    _DRAW_MASK = False
    if _DRAW_MASK and label_map is not None and label_map.any():
        lm = label_map
        if lm.shape != (rows, cols):
            lm = cv2.resize(lm, (cols, rows), interpolation=cv2.INTER_NEAREST)
        overlay = vis.copy()
        for lab in np.unique(lm):
            if lab == 0:        # 0 = 背景
                continue
            overlay[lm == lab] = color_for_label(int(lab))
        vis = cv2.addWeighted(overlay, mask_alpha, vis, 1.0 - mask_alpha, 0)

    # 2) 画每个缺陷的外接矩形 —— ★ 框向外扩 box_gap 再画 ★
    #    小缺陷只有十几像素时，紧贴 bbox 的粗描边会把缺陷整个盖住看不清。
    #    向外扩一圈留缝，让缺陷完整露在框内部、线条不压到缺陷本体。
    #    线宽设上限（≤4），避免大图上 //800 算出过粗的边。
    rect_thickness = max(2, min(max(rows, cols) // 800, 4))
    box_gap = rect_thickness + 5             # 框与缺陷之间的留白（比线宽大）
    for d in defects:
        x, y, w, h = d.bbox
        if d.class_name in ng_trigger_classes:
            color = (0, 0, 255)             # 红 = NG 触发
        elif d.class_name:
            color = (0, 200, 255)           # 黄 = 非 NG 类
        else:
            color = (200, 200, 200)         # 灰 = 未分类
        x0 = max(0, x - box_gap)
        y0 = max(0, y - box_gap)
        x1 = min(cols - 1, x + w + box_gap)
        y1 = min(rows - 1, y + h + box_gap)
        cv2.rectangle(vis, (x0, y0), (x1, y1), color, rect_thickness)

    # 3) 标类别名 — 所有文本一次 PIL 转换内画完（之前每个缺陷各转一次，
    #    10 个缺陷 = 10 次 BGR↔RGB 抖动，每次 ~10ms 在 3072×1024 上）
    font_size = max(18, max(rows, cols) // 100)
    text_items = []                              # [(xy, text, color, bg)]
    for d in defects:
        x, y, _w, _h = d.bbox
        if d.class_name in ng_trigger_classes:
            txt_color = (255, 255, 255); bg = (0, 0, 255)
        elif d.class_name:
            txt_color = (0, 0, 0); bg = (0, 200, 255)
        else:
            txt_color = (255, 255, 255); bg = (100, 100, 100)
        # 图上只标类别名；置信度/长度/面积进数据/MES/日志，不挤在框上（防遮挡+出界）
        label = d.class_name or "未分类"
        # 文字贴在「外扩后框」的左上角上方，跟框对齐（出界由 _put_texts_batch 兜底夹紧）
        tx = max(0, x - box_gap)
        ty = max(0, y - box_gap - font_size - 6)
        text_items.append(((tx, ty), label, txt_color, bg))
    if text_items:
        vis = _put_texts_batch(vis, text_items, font_size=font_size)

    return vis


def draw_marked_crop(crop_gray: np.ndarray,
                      defect,
                      *,
                      mask_alpha: float = 0.4) -> np.ndarray:
    """在缺陷小图上画轮廓 + 类别标签。

    crop 是从大图按 padding 裁出来的方形小图，缺陷大致在中间。
    我们用阈值简单估计缺陷区域（避免再调 seg 模型）做半透明色块叠加。
    """
    vis = to_bgr(crop_gray)
    rows, cols = crop_gray.shape[:2]

    # mask 染色已禁用（按现场要求：只要类别名标签，不要半透明色块）
    # 保留阈值估计代码但不再渲染。如需恢复，把下面 _DRAW_MASK 改 True。
    _DRAW_MASK = False
    if _DRAW_MASK:
        mean_g = float(crop_gray.mean())
        std_g  = float(crop_gray.std())
        dark_thr = max(0, mean_g - 1.5 * std_g)
        mask = (crop_gray < dark_thr).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        if mask.any():
            if defect.class_name:
                color = color_for_label(defect.class_name)
            else:
                color = (0, 200, 255)
            overlay = vis.copy()
            overlay[mask > 0] = color
            vis = cv2.addWeighted(overlay, mask_alpha, vis, 1.0 - mask_alpha, 0)

    # 小图上只标类别名（小图本就在缺陷库/存档里看，数值另有字段）
    if defect.class_name:
        label = defect.class_name
        font_size = max(14, min(rows, cols) // 12)
        vis = _put_text(vis, label, (4, 4), font_size=font_size,
                         color=(255, 255, 255), bg=(0, 0, 0))
    return vis
