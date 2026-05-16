"""
不同 judge 参数下扫 54 张生产图 — 找边界 / 极端值 bug
====================================================
矩阵：6 套 judge 配置 × 54 张图。每套：

  permissive    : 全宽松（report_ng=False 全部，应 0 NG）
  default       : 当前默认（仅 隐裂 NG）
  strict_area   : 所有类 report_ng=True + max_area=1（应几乎全 NG）
  strict_count  : max_count=0（任意 count > 0 即 NG）
  high_conf     : min_confidence=0.999（几乎全过滤掉 → 0 NG）
  zero_thr      : max_area=0 + max_length=0（任意非零即 NG）

每套统计：
  - OK / NG / 异常数
  - 是否每张图都有相同分类（class 一致性）
  - 耗时分布（参数变化不应明显改变耗时）

bug 信号：
  - 异常 > 0 → 边界值导致代码 crash
  - permissive 出现 NG → report_ng=False 没生效
  - high_conf 出现 NG → min_confidence 过滤没生效
  - strict_area 0 NG 而 cls 有 "其他" 候选 → report_ng=True 没生效
  - zero_thr 0 NG → 严格不等号方向反了
"""

from __future__ import annotations

import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8, imread_safe
setup_console_utf8()

import cv2
import numpy as np

from sirod_inspector.algorithm import Pipeline, JudgeConfig
from sirod_inspector.algorithm.judge import ClassRule
from sirod_inspector.core.tcp_server import InspectData
from main_camera import save_inspect_images


SEG_MODEL = str(_REPO_ROOT / "models" / "Model_seg.m")
CLS_MODEL = str(_REPO_ROOT / "models" / "Model_cls.m")

CLASSES = ["隐裂", "崩边", "凹坑", "杂质", "刀痕",
           "气泡", "划痕", "其他"]


def make_rule_set(name: str) -> list:
    if name == "permissive":
        return [ClassRule(c, report_ng=False,
                          max_area=1e9, max_length=1e9,
                          max_count=1_000_000, min_confidence=0.0)
                for c in CLASSES]
    if name == "default":
        return [ClassRule(c, report_ng=(c == "隐裂"),
                          max_area=10.0 if c == "隐裂" else 100.0,
                          max_length=2.0 if c == "隐裂" else 5.0,
                          max_count=10, min_confidence=0.0)
                for c in CLASSES]
    if name == "strict_area":
        return [ClassRule(c, report_ng=True,
                          max_area=1.0, max_length=1e9,
                          max_count=1_000_000, min_confidence=0.0)
                for c in CLASSES]
    if name == "strict_count":
        return [ClassRule(c, report_ng=True,
                          max_area=1e9, max_length=1e9,
                          max_count=0, min_confidence=0.0)
                for c in CLASSES]
    if name == "high_conf":
        return [ClassRule(c, report_ng=True,
                          max_area=0, max_length=0,
                          max_count=0, min_confidence=0.999)
                for c in CLASSES]
    if name == "zero_thr":
        return [ClassRule(c, report_ng=True,
                          max_area=0.0, max_length=0.0,
                          max_count=0, min_confidence=0.0)
                for c in CLASSES]
    raise ValueError(name)


def run_one_set(name: str, pipeline: Pipeline,
                rules: list, paths: list, tmpdir: Path) -> dict:
    """对一套规则跑完整 54 张，返回统计"""
    pipeline.class_rules = list(rules)

    base_dir = tmpdir / "images"
    raw_tif_dir = tmpdir / "ImageRaw"
    web_image_dir = tmpdir / "WebImage"

    results = Counter()
    cls_dist = Counter()
    ng_classes = Counter()
    exceptions = []
    cts = []

    for path in paths:
        try:
            img = imread_safe(str(path), cv2.IMREAD_UNCHANGED)
            if img is None:
                raise RuntimeError(f"读图失败 {path}")
            t0 = time.perf_counter()
            r = pipeline.process(
                img, keep_processed_image=True, keep_crops=True,
                keep_label_map=True, keep_raw_input=True,
            )
            cts.append((time.perf_counter() - t0) * 1000)

            results[r.result] += 1
            if r.defect_type:
                ng_classes[r.defect_type] += 1
            for d in r.defects:
                cls_dist[d.class_name or "(未分类)"] += 1

            # 仍走 save_inspect_images 防 save path 在不同参数下出问题
            data = InspectData(
                rod_id=path.stem.split("_", 1)[-1][:30] or "T",
                result=r.result, image=r.processed_image,
                defect_count=r.defect_count,
                raw_json={"defects": []},
            )
            save_inspect_images(
                data, r,
                base_dir=str(base_dir),
                raw_tif_dir=str(raw_tif_dir),
                web_image_dir=str(web_image_dir),
                web_url_base="",
            )
        except Exception as e:
            exceptions.append((path.name, str(e), traceback.format_exc()))

    return {
        "results": results, "cls_dist": cls_dist,
        "ng_classes": ng_classes, "exceptions": exceptions,
        "cts": cts,
    }


def main() -> int:
    print("=" * 70)
    print("Judge 参数矩阵扫 54 张生产图 — 边界 / 极端值巡检")
    print("=" * 70)

    src_dir = _REPO_ROOT / "source_image"
    paths = sorted(src_dir.glob("*.tif"))
    print(f"输入: {len(paths)} 张 .tif")

    print("加载 Pipeline ...")
    pipeline = Pipeline(SEG_MODEL, CLS_MODEL, JudgeConfig())
    print("就绪\n")

    sets = ["permissive", "default", "strict_area",
            "strict_count", "high_conf", "zero_thr"]
    all_stats = {}

    import shutil
    with tempfile.TemporaryDirectory(prefix="judge_matrix_") as td:
        td_path = Path(td)
        for name in sets:
            rules = make_rule_set(name)
            print(f"── [{name}] ──")
            t0 = time.perf_counter()
            stats = run_one_set(name, pipeline, rules, paths, td_path)
            elapsed = time.perf_counter() - t0
            all_stats[name] = stats
            # 每套跑完先清盘再开下一套 — 6 套 × 54 × 30MB TIF = 10GB peak，
            # 不清会撑爆硬盘
            for sub in ("images", "ImageRaw", "WebImage"):
                d = td_path / sub
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
            ok = stats["results"].get("OK", 0)
            ng = stats["results"].get("NG", 0)
            exc = len(stats["exceptions"])
            cts = stats["cts"]
            avg = sum(cts) / len(cts) if cts else 0
            mx = max(cts) if cts else 0
            print(f"  OK={ok:3d}  NG={ng:3d}  EXC={exc:3d}  "
                  f"avg={avg:.0f}ms  max={mx:.0f}ms  ({elapsed:.0f}s)")
            if stats["ng_classes"]:
                print(f"  NG 类: {dict(stats['ng_classes'])}")
            if stats["cls_dist"]:
                print(f"  分类: {dict(stats['cls_dist'])}")
            if stats["exceptions"]:
                print(f"  异常: {stats['exceptions'][0][1]}")
            print()

    pipeline.close()

    # ─── 跨参数集一致性 / 预期对照 ───
    print("=" * 70)
    print("跨配置一致性 / 预期检查")
    print("=" * 70)
    issues = []

    # 1) 异常总数应为 0
    total_exc = sum(len(s["exceptions"]) for s in all_stats.values())
    if total_exc > 0:
        issues.append(f"总异常 {total_exc}（边界值导致 crash）")
    else:
        print(f"  [OK] 所有参数集 0 异常 — 边界值不 crash")

    # 2) permissive 不应有 NG（report_ng=False 全部）
    perm_ng = all_stats["permissive"]["results"].get("NG", 0)
    if perm_ng > 0:
        issues.append(f"permissive: NG={perm_ng}（report_ng=False 没生效）")
    else:
        print(f"  [OK] permissive 0 NG — report_ng=False 生效")

    # 3) high_conf 不应有 NG（min_confidence=0.999 几乎全过滤）
    hc_ng = all_stats["high_conf"]["results"].get("NG", 0)
    # 实际可能有 conf 接近 1.0 的 → 允许少量；但大部分应 OK
    if hc_ng > len(paths) // 2:
        issues.append(f"high_conf: NG={hc_ng}（min_confidence 没过滤）")
    else:
        print(f"  [OK] high_conf NG={hc_ng}/{len(paths)} — "
              f"min_confidence 至少过滤了大部分")

    # 4) strict_area: max_area=1 → 任何 area>1 的都该 NG
    sa_ng = all_stats["strict_area"]["results"].get("NG", 0)
    sa_cls = all_stats["strict_area"]["cls_dist"]
    candidates = sum(sa_cls.values())
    if candidates > 0 and sa_ng == 0:
        issues.append(f"strict_area: {candidates} 个候选但 0 NG（max_area=1 没生效）")
    else:
        print(f"  [OK] strict_area NG={sa_ng}，候选 {candidates}")

    # 5) zero_thr: max_area=0 → 任意 area>0 都 NG（数值上 0 < 任何 area > 0）
    zt_ng = all_stats["zero_thr"]["results"].get("NG", 0)
    zt_cls = all_stats["zero_thr"]["cls_dist"]
    zt_cand = sum(zt_cls.values())
    # 跟 strict_area 应该差不多（max_area=0 比 1 更严）
    if zt_cand > 0 and zt_ng == 0:
        issues.append(f"zero_thr: {zt_cand} 个候选但 0 NG（max_area=0 边界处理错了）")
    else:
        print(f"  [OK] zero_thr NG={zt_ng}/{len(paths)}")

    # 6) 耗时一致性：参数应该不影响整体耗时显著（同样的 seg+cls）
    avgs = [sum(s["cts"])/len(s["cts"]) for s in all_stats.values() if s["cts"]]
    if avgs and max(avgs) > min(avgs) * 1.5:
        issues.append(f"耗时差异 > 50%: {[f'{a:.0f}' for a in avgs]}")
    else:
        print(f"  [OK] 耗时跨配置稳定: "
              f"{[f'{a:.0f}ms' for a in avgs]}")

    print()
    if issues:
        print(f"=== 发现 {len(issues)} 个潜在 bug ===")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    print("[OK] judge 参数矩阵全通")
    return 0


if __name__ == "__main__":
    sys.exit(main())
