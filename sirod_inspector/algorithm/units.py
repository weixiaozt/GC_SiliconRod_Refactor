"""
像素 → 毫米 单位换算（显示/上报层）
=====================================
内部算法、judge 阈值、DB 都仍用像素；只有在给人看 / 上报 MES 时才换算成 mm。

约定
----
- ``pixels_per_mm`` (ppm)：每毫米对应多少像素，由现场用标准件标定。
  例：10 个像素 = 1mm → ppm = 10。
- **长度** 显示值 = 外接圆「直径」= ``2 × outer_radius`` 再除以 ppm。
  （内部 ``outer_radius`` 仍是「半径」，judge 阈值不动。）
- **面积** 换算是长度换算的「平方」：1mm² = ppm² 个像素，故 area_mm² = area_px / ppm²。

``ppm <= 0`` 视为「未标定」，统一返回 0.0，避免除零和误导现场。
"""

from __future__ import annotations


def radius_px_to_length_mm(outer_radius_px: float, pixels_per_mm: float) -> float:
    """外接圆半径(px) → 长度(mm)。长度定义 = 直径 = 2×半径。"""
    if pixels_per_mm <= 0:
        return 0.0
    return 2.0 * float(outer_radius_px) / float(pixels_per_mm)


def area_px_to_mm2(area_px: float, pixels_per_mm: float) -> float:
    """像素面积 → mm²。注意是 ppm 的平方（面积是长度的二次量）。"""
    if pixels_per_mm <= 0:
        return 0.0
    return float(area_px) / (float(pixels_per_mm) ** 2)
