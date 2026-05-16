"""
原图预处理：消除光源分时频闪造成的明暗行
==========================================
线扫相机在光源 3 相轮换下采集，原图行均值呈 "暗-中-亮" 3 行一周期循环。
直接喂给检测算法会被条纹干扰，需要先压平。

预处理链路（与 Halcon Run.hdev/Run + 01.hdev 等价）::

    [uint16, 15000x1024]                       原图（高 x 宽）
        │
        │ 1) 在原图采样 ROI 上计算削顶阈值 V
        │    （取暗行灰度均值的最大值）
        │
        │ 2) 纵向 1/3 缩放 → 三行均值，明暗被平均掉
        ▼
    [uint16, 5000x1024]
        │ 3) 削顶: image = min(image, 1.3*V)
        │ 4) 拉伸到 uint8 全动态范围
        ▼
    [uint8,  5000x1024]
        │ 5) 顺时针 90° 旋转
        ▼
    [uint8,  1024x5000]
        │ 6) 缩放到 AI 模型输入尺寸
        ▼
    [uint8,  1024x3072]   ← 检测、存图、上传均基于此

公开 API
--------
    preprocess(image_uint16) -> np.ndarray    一步到位输出 1024x3072 uint8
    compute_clip_threshold(image_uint16) -> float    单独计算削顶阈值（调试用）

参数常量
--------
所有阈值与 Halcon 端 hardcode 完全一致，便于现场对照。
"""

from __future__ import annotations

import logging
from typing import Tuple

import cv2
import numpy as np

logger = logging.getLogger("SiRod.Preprocess")


# ============================================================
# 常量（与 Halcon 端 hardcode 对齐）
# ============================================================

# 采样 ROI 配置
_ROI_BAND_HEIGHT = 100              # 每条采样带的高度（像素行）
_ROI_BAND_COUNT = 49                # 条带数：从 row 0 到 row 4800，每 100 行一条
_ROI_COL_EROSION = 640              # 列方向腐蚀核宽（保留中段约 384 列）

# 暗区筛选阈值
_DARK_GRAY_UPPER = 12000            # 灰度 <= 12000 视为 "暗区"
_MIN_REGION_HEIGHT = 99             # 暗区连通块最小高度
_MIN_REGION_AREA = 380 * 100        # 暗区连通块最小面积 (= 38000)

# 削顶系数
_CLIP_FACTOR = 1.3                  # threshold = V * 1.3

# 缩放 & 输出尺寸
_VERTICAL_SHRINK = 1.0 / 3.0        # 纵向缩放比（3 行合并为 1 行）
_AI_INPUT_HEIGHT = 1024             # AI 模型输入高度
_AI_INPUT_WIDTH = 3072              # AI 模型输入宽度

# 兜底阈值：暗区筛选失败时使用
_FALLBACK_CLIP_VALUE = 15000        # 与 Halcon `Median_Tuple` 中 catch 分支一致


# ============================================================
# 内部辅助：枚举采样 ROI 条带
# ============================================================

def _iter_sampling_bands(rows: int, cols: int):
    """生成采样 ROI 条带的 (row0, row1, col0, col1) 区间。

    对应 Halcon 端::

        gen_rectangle1 (Rectangle, 0, 0, 100, 1024)
        for Index1 := 100 to 4800 by 100:
            gen_rectangle1 (Rectangle1, Index1, 0, Index1+100, 1024)
            concat_obj (Rectangle, Rectangle1, Rectangle)
        endfor
        erosion_rectangle1 (Rectangle, RegionErosion, 640, 1)

    每 100 行是**独立的 region**（Halcon 用 concat_obj 串多个矩形），
    列方向腐蚀 640（保留中央 ~384 列）。每条带各自做后续连通块分析。
    """
    # 列方向腐蚀：左右各裁 _ROI_COL_EROSION/2
    half_erode = _ROI_COL_EROSION // 2
    col0 = half_erode
    col1 = cols - half_erode
    if col1 <= col0:
        col0, col1 = 0, cols

    band_count = _ROI_BAND_COUNT + 1   # row 0..4900，共 49 条 + 起始 1 条
    for i in range(band_count):
        row0 = i * _ROI_BAND_HEIGHT
        row1 = min(row0 + _ROI_BAND_HEIGHT, rows)
        if row0 >= rows:
            break
        yield row0, row1, col0, col1


# ============================================================
# 公开 API
# ============================================================

def compute_clip_threshold(image_uint16: np.ndarray) -> float:
    """估算削顶阈值（暗行灰度均值的最大值）。

    对应 Halcon 端 ``Median_Tuple``::

        threshold (Image, Regions, 0, 12000)     # 找暗区
        intersection (RegionErosion, Regions)    # 与采样 ROI 取交
        select_shape (..., 'height', 99, ...)    # 筛高度
        select_shape (..., 'area', 38000, ...)   # 筛面积
        gray_features (..., 'mean')              # 算灰度均值
        tuple_max (...)                          # 取最大

    Parameters
    ----------
    image_uint16 : np.ndarray
        15000 x 1024 的 uint16 原图（H x W）。

    Returns
    -------
    float
        削顶阈值 V。计算失败时返回 ``_FALLBACK_CLIP_VALUE`` (15000)。
    """
    if image_uint16.dtype != np.uint16:
        raise TypeError(
            f"expected uint16 image, got {image_uint16.dtype}"
        )
    if image_uint16.ndim != 2:
        raise ValueError(
            f"expected 2D grayscale image, got shape {image_uint16.shape}"
        )

    rows, cols = image_uint16.shape

    # 每个 ROI 条带独立处理 — 对应 Halcon 端 concat_obj 串多个矩形 region
    # 再 intersection / select_shape / gray_features 逐 region 算属性
    means = []
    for row0, row1, col0, col1 in _iter_sampling_bands(rows, cols):
        band = image_uint16[row0:row1, col0:col1]
        dark_mask = (band <= _DARK_GRAY_UPPER).astype(np.uint8)
        if not dark_mask.any():
            continue

        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            dark_mask, connectivity=8
        )
        for lbl in range(1, num_labels):
            _x, _y, _w, h, area = stats[lbl]
            if h <= _MIN_REGION_HEIGHT:
                continue
            if area <= _MIN_REGION_AREA:
                continue
            region_pixels = band[labels == lbl]
            means.append(float(region_pixels.mean()))

    if not means:
        logger.info(
            "暗区连通块筛选后为空（图像可能整体过亮，无明显明暗条纹），"
            f"使用兜底阈值 {_FALLBACK_CLIP_VALUE}"
        )
        return float(_FALLBACK_CLIP_VALUE)

    V = max(means)
    logger.debug(
        f"削顶阈值估算: 候选 {len(means)} 块，"
        f"max={V:.1f}, all={[f'{m:.0f}' for m in sorted(means)]}"
    )
    return V


def preprocess(image_uint16: np.ndarray) -> np.ndarray:
    """原图 → AI 输入尺寸的预处理。

    Parameters
    ----------
    image_uint16 : np.ndarray
        15000 x 1024 uint16 原图（H x W）。

    Returns
    -------
    np.ndarray
        1024 x 3072 uint8 处理后图（H x W）。可直接喂给分割 / 分类模型。
    """
    if image_uint16.dtype != np.uint16:
        raise TypeError(f"expected uint16 image, got {image_uint16.dtype}")
    if image_uint16.ndim != 2:
        raise ValueError(f"expected 2D image, got shape {image_uint16.shape}")

    rows, cols = image_uint16.shape

    # 1) 在原图上估阈值
    V = compute_clip_threshold(image_uint16)
    clip_max = V * _CLIP_FACTOR

    # 2) 纵向 1/3 缩放（INTER_AREA = 块均值，等价 3 行合并）
    new_rows = max(1, int(round(rows * _VERTICAL_SHRINK)))
    shrunk = cv2.resize(
        image_uint16, (cols, new_rows), interpolation=cv2.INTER_AREA
    )

    # 3) 削顶
    clipped = np.minimum(shrunk, clip_max).astype(np.uint16)

    # 4) 拉伸到 uint8 全动态范围（对应 Halcon scale_image_max）
    cmin, cmax = int(clipped.min()), int(clipped.max())
    if cmax > cmin:
        normalized = ((clipped.astype(np.float32) - cmin)
                      / (cmax - cmin) * 255.0).clip(0, 255).astype(np.uint8)
    else:
        normalized = np.zeros_like(clipped, dtype=np.uint8)

    # 5) 顺时针 90° 旋转：5000x1024 → 1024x5000
    rotated = cv2.rotate(normalized, cv2.ROTATE_90_CLOCKWISE)

    # 6) 缩到 AI 输入尺寸 1024x3072
    final = cv2.resize(
        rotated, (_AI_INPUT_WIDTH, _AI_INPUT_HEIGHT),
        interpolation=cv2.INTER_AREA,
    )

    logger.debug(
        f"预处理完成: {image_uint16.shape} uint16 → "
        f"{final.shape} uint8, V={V:.0f}, clip={clip_max:.0f}"
    )
    return final


def preprocess_with_debug(image_uint16: np.ndarray) -> Tuple[np.ndarray, dict]:
    """同 preprocess，但额外返回调试信息（V、统计、各中间形状）。"""
    info: dict = {}
    rows, cols = image_uint16.shape
    info["input_shape"] = (rows, cols)
    info["input_dtype"] = str(image_uint16.dtype)
    info["input_min"] = int(image_uint16.min())
    info["input_max"] = int(image_uint16.max())
    info["input_mean"] = float(image_uint16.mean())

    V = compute_clip_threshold(image_uint16)
    info["clip_V"] = V
    info["clip_max"] = V * _CLIP_FACTOR

    result = preprocess(image_uint16)
    info["output_shape"] = result.shape
    info["output_dtype"] = str(result.dtype)
    info["output_min"] = int(result.min())
    info["output_max"] = int(result.max())
    info["output_mean"] = float(result.mean())
    return result, info
