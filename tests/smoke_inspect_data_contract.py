"""
InspectData 消费契约测试
========================
验证 ``detection_to_inspect_data()`` 产出的对象，包含
``main.py._handle_tcp_data`` 现有消费链路所需的所有字段。

这相当于"main_camera.py 的核心 contract"检查 — 不依赖 PyQt6，
所以即使 UI 库装不上也能跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sirod_inspector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()

import numpy as np

from sirod_inspector.algorithm.pipeline import (
    DetectionResult, ClassifiedDefect,
)
from sirod_inspector.core.inspect_engine import detection_to_inspect_data


def main() -> int:
    print("=" * 60)
    print("InspectData 消费契约测试")
    print("=" * 60)

    # 1) 构造一个典型的 DetectionResult（NG 隐裂场景）
    result = DetectionResult(
        result="NG", quality=1, defect_type="隐裂",
        defect_count=1,
        max_area=8620.0, sum_area=8620.0,
        max_length=137.8,
        processed_image=np.zeros((1024, 3072), dtype=np.uint8),
        defects=[ClassifiedDefect(
            bbox=(2183, 773, 121, 251),
            area=8620, outer_radius=137.8,
            class_name="隐裂", class_confidence=0.58,
        )],
        ct_ms=503.0,
        judge_reasons=["max_area=8620>thr(10)",
                       "sum_area=8620>thr(10)",
                       "max_length=137.80>thr(2.00)"],
    )

    # 2) 装配
    data = detection_to_inspect_data(
        result, rod_id="ROD20260516001", inspect_id=42,
    )

    # 3) 列举 main.py._handle_tcp_data 消费的字段（实际通过 grep 提取）
    required_fields = [
        ("rod_id", str),
        ("result", str),
        ("image", (np.ndarray, type(None))),
        ("defect_type", str),
        ("defect_count", int),
        ("timestamp", str),
        ("inspect_id", int),
        ("quality", int),
        ("max_area", (int, float)),
        ("total_area", (int, float)),
        ("max_length", (int, float)),
        ("ct", (int, float)),
        ("check_time", str),
        ("upload_time", str),
        ("raw_json", dict),
    ]

    print("\n[字段检查]")
    all_ok = True
    for name, expected_type in required_fields:
        if not hasattr(data, name):
            print(f"  缺字段: {name}")
            all_ok = False
            continue
        value = getattr(data, name)
        if isinstance(expected_type, tuple):
            ok = isinstance(value, expected_type) or value is None
        else:
            ok = isinstance(value, expected_type)
        flag = "OK " if ok else "BAD"
        repr_v = repr(value) if not isinstance(value, np.ndarray) \
                  else f"<ndarray {value.shape} {value.dtype}>"
        if len(repr_v) > 60:
            repr_v = repr_v[:57] + "..."
        print(f"  [{flag}] {name:<14s} ({expected_type if not isinstance(expected_type, tuple) else 'multi'}) = {repr_v}")
        if not ok:
            all_ok = False

    # 4) 字段间一致性
    print("\n[一致性检查]")
    consistency = [
        ("quality 1 ↔ result NG",  (data.quality == 1) == (data.result == "NG")),
        ("rod_id 注入正确",         data.rod_id == "ROD20260516001"),
        ("inspect_id 注入正确",     data.inspect_id == 42),
        ("ct = ct_ms / 1000",       abs(data.ct - 0.503) < 1e-6),
        ("total_area = sum_area",   data.total_area == result.sum_area),
        ("max_length 直传",          data.max_length == result.max_length),
        ("defect_type 直传",         data.defect_type == result.defect_type),
        ("raw_json.judge_reasons 存在",
         bool(data.raw_json.get("judge_reasons"))),
        ("raw_json.defects 长度匹配",
         len(data.raw_json.get("defects", [])) == len(result.defects)),
    ]
    for desc, ok in consistency:
        print(f"  [{'OK ' if ok else 'BAD'}] {desc}")
        if not ok:
            all_ok = False

    # 5) OK 场景
    print("\n[OK 场景]")
    result_ok = DetectionResult(
        result="OK", quality=0, defect_type="",
        defect_count=0,
        processed_image=np.zeros((1024, 3072), dtype=np.uint8),
        ct_ms=480.0,
    )
    data_ok = detection_to_inspect_data(
        result_ok, rod_id="ROD_OK_001", inspect_id=1,
    )
    print(f"  rod_id={data_ok.rod_id} result={data_ok.result} "
          f"quality={data_ok.quality} defect_count={data_ok.defect_count} "
          f"max_area={data_ok.max_area}")
    print(f"  judge_reasons in raw_json: "
          f"{data_ok.raw_json.get('judge_reasons', '?')}")

    print("\n" + ("[OK] 契约测试通过" if all_ok else "[FAIL] 契约测试有问题"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
