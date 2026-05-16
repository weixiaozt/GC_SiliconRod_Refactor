"""
端到端检测流水线烟雾测试
========================
跑 source_image/*.tif → preprocess → seg → 连通块 → 裁剪 → cls → 判定
并写一份带标注的可视化图到 tests/outputs/pipeline/。

用法
----
    # 跑全部 source_image
    python tests/smoke_pipeline.py

    # 指定单张
    python tests/smoke_pipeline.py --image path/to/xxx.tif

    # 自定义模型
    python tests/smoke_pipeline.py --seg path/to/Model_seg.m \\
                                   --cls path/to/Model_cls.m
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imwrite_safe, imread_safe
setup_console_utf8()

import cv2
import numpy as np

from sirod_inspector.algorithm import (
    DetectionResult,
    JudgeConfig,
    Pipeline,
)


# ──────── 中文文字渲染（cv2.putText 不支持中文，用 PIL） ────────
_FONT_CACHE: dict = {}


def _get_font(size: int):
    """缓存系统中文字体"""
    from PIL import ImageFont
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
        r"C:\Windows\Fonts\simhei.ttf",    # 黑体
        r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    ]
    for p in candidates:
        if Path(p).is_file():
            font = ImageFont.truetype(p, size)
            _FONT_CACHE[size] = font
            return font
    font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def put_text(img_bgr: np.ndarray, text: str, xy: tuple,
             font_size: int = 18,
             color: tuple = (255, 255, 255),
             bg_color: tuple | None = None,
             pad: int = 3) -> np.ndarray:
    """在 BGR 图上写支持中文的文字，可选背景填充。"""
    from PIL import Image, ImageDraw
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    font = _get_font(font_size)
    x, y = xy
    bbox = draw.textbbox((x, y), text, font=font)
    if bg_color is not None:
        draw.rectangle(
            (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad),
            fill=(bg_color[2], bg_color[1], bg_color[0]),  # BGR → RGB
        )
    draw.text((x, y), text, fill=(color[2], color[1], color[0]), font=font)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

# 默认查找位置
_DEFAULT_SEG = _REPO_ROOT / "models" / "Model_seg.m"
_DEFAULT_CLS = _REPO_ROOT / "models" / "Model_cls.m"
_DEFAULT_INPUT_DIR = _REPO_ROOT / "source_image"
_DEFAULT_OUTPUT_DIR = _REPO_ROOT / "tests" / "outputs" / "pipeline"


def draw_overlay(img_uint8: np.ndarray, result: DetectionResult) -> np.ndarray:
    """在预处理图上画缺陷外接框 + 顶部摘要。仅作调试可视化。"""
    from sirod_inspector.algorithm import NG_TRIGGER_CLASSES

    if img_uint8.ndim == 2:
        vis = cv2.cvtColor(img_uint8, cv2.COLOR_GRAY2BGR)
    else:
        vis = img_uint8.copy()

    box_color = (0, 0, 255) if result.result == "NG" else (0, 200, 0)

    # 先画所有框（cv2.rectangle 不涉及中文，可以正常用）
    for d in result.defects:
        x, y, w, h = d.bbox
        if d.class_name in NG_TRIGGER_CLASSES:
            color = (0, 0, 255)      # 红：触发 NG
        elif d.class_name:
            color = (0, 200, 255)    # 黄：分类为非 NG 缺陷
        else:
            color = (200, 200, 200)  # 灰：未分类
        cv2.rectangle(vis, (x, y), (x + w, y + h), color, 2)

    # 再用 PIL 一次性叠所有中文标签（一次 BGR↔RGB 转换避免反复抖动）
    for d in result.defects:
        x, y, w, h = d.bbox
        if d.class_name in NG_TRIGGER_CLASSES:
            color = (255, 255, 255); bg = (0, 0, 255)
        elif d.class_name:
            color = (0, 0, 0); bg = (0, 200, 255)
        else:
            color = (255, 255, 255); bg = (100, 100, 100)
        label = (f"{d.class_name} {d.class_confidence:.2f}"
                 if d.class_name else f"a={d.area} r={d.outer_radius:.1f}")
        # 标签放框左上角上方
        ty = max(0, y - 24)
        vis = put_text(vis, label, (x, ty),
                       font_size=18, color=color, bg_color=bg)

    # 顶部摘要条
    summary = (f"{result.result}  类型={result.defect_type or '-'}  "
               f"个数={result.defect_count}  "
               f"max_area={result.max_area:.0f}  "
               f"sum_area={result.sum_area:.0f}  "
               f"max_len={result.max_length:.1f}  "
               f"ct={result.ct_ms:.0f}ms")
    vis = put_text(vis, summary, (10, 4),
                   font_size=20, color=box_color, bg_color=(0, 0, 0))
    return vis


def _safe_filename(s: str) -> str:
    """把分类名里可能的不合法字符替换掉，方便做文件名。"""
    for ch in r'<>:"/\|?*':
        s = s.replace(ch, "_")
    return s.strip() or "unknown"


def run_one(pipeline: Pipeline, img_path: Path, out_dir: Path) -> int:
    img = imread_safe(img_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        print(f"  [SKIP] cv2.imread 失败: {img_path.name}")
        return 1

    t0 = time.perf_counter()
    result = pipeline.process(img, keep_crops=True)
    dt_ms = (time.perf_counter() - t0) * 1000

    badge = "NG" if result.result == "NG" else "OK"
    print(f"  {img_path.name}: {badge}  "
          f"type={result.defect_type or '-':<6s}  "
          f"count={result.defect_count}  "
          f"max_area={result.max_area:.0f}  "
          f"sum_area={result.sum_area:.0f}  "
          f"max_len={result.max_length:.1f}  "
          f"ct={dt_ms:.0f}ms")
    if result.judge_reasons:
        print(f"     judge:  {'; '.join(result.judge_reasons)}")
    for i, d in enumerate(result.defects):
        if d.class_name:
            print(f"     [{i}] {d.class_name} ({d.class_confidence:.3f})  "
                  f"bbox={d.bbox}  area={d.area}  r={d.outer_radius:.1f}")

    # 写整图标注可视化
    if result.processed_image is not None:
        vis = draw_overlay(result.processed_image, result)
        out_path = out_dir / f"{img_path.stem}_{badge}.png"
        imwrite_safe(out_path, vis, ext=".png")

    # 写每个缺陷的 crop 小图
    if any(d.crop is not None for d in result.defects):
        crops_dir = out_dir / "crops" / img_path.stem
        crops_dir.mkdir(parents=True, exist_ok=True)
        for i, d in enumerate(result.defects):
            if d.crop is None:
                continue
            cls_name = _safe_filename(d.class_name) if d.class_name else "uncls"
            crop_name = (f"d{i:02d}_{cls_name}_"
                         f"{d.class_confidence:.2f}_"
                         f"a{d.area}_r{d.outer_radius:.0f}.bmp")
            crop_path = crops_dir / crop_name
            imwrite_safe(crop_path, d.crop, ext=".bmp")
        print(f"     crops -> {crops_dir} ({len(result.defects)} 张)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="端到端检测流水线 smoke test")
    ap.add_argument("--seg", default=str(_DEFAULT_SEG))
    ap.add_argument("--cls", default=str(_DEFAULT_CLS))
    ap.add_argument("--image", help="单张图路径（默认遍历 source_image/*.tif）")
    ap.add_argument("--input-dir", default=str(_DEFAULT_INPUT_DIR))
    ap.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_DIR))
    ap.add_argument("--max-area", type=float, default=10.0)
    ap.add_argument("--sum-area", type=float, default=10.0)
    ap.add_argument("--max-count", type=int, default=10)
    ap.add_argument("--max-length", type=float, default=2.0)
    args = ap.parse_args()

    seg_path = Path(args.seg).resolve()
    cls_path = Path(args.cls).resolve()
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not seg_path.is_file():
        print(f"[FAIL] seg 模型不存在: {seg_path}")
        return 1
    if not cls_path.is_file():
        print(f"[FAIL] cls 模型不存在: {cls_path}")
        return 1

    if args.image:
        targets = [Path(args.image).resolve()]
    else:
        input_dir = Path(args.input_dir).resolve()
        if not input_dir.is_dir():
            print(f"[FAIL] 输入目录不存在: {input_dir}")
            return 1
        targets = sorted(input_dir.glob("*.tif")) + sorted(input_dir.glob("*.bmp"))

    if not targets:
        print(f"[FAIL] 没有可处理的图片")
        return 1

    judge_cfg = JudgeConfig(
        max_area=args.max_area, sum_area=args.sum_area,
        max_count=args.max_count, max_length=args.max_length,
    )

    print("=" * 60)
    print("端到端检测流水线 smoke test")
    print("=" * 60)
    print(f"seg     : {seg_path}")
    print(f"cls     : {cls_path}")
    print(f"judge   : {judge_cfg}")
    print(f"output  : {out_dir}")
    print(f"targets : {len(targets)} 张")
    print()

    with Pipeline(seg_path, cls_path, judge_cfg) as pipeline:
        print("─── 开始处理 ───")
        for p in targets:
            run_one(pipeline, p, out_dir)

    print()
    print(f"[OK] 完成，可视化结果在 {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
