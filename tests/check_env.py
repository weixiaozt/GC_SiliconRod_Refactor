"""
工厂机部署预检
================
在新机器第一次跑 main_camera.py 前用这个脚本逐项确认环境：

  ✓ Python 版本 ≥ 3.10
  ✓ 必需 Python 包
  ✓ AI 推理 DLL 位置
  ✓ 模型文件
  ✓ BV 相机驱动 DLL
  ✓ 模型加载与单次推理
  ✓ 相机枚举与单次抓图
  ✓ config.json 关键字段
  ✓ 扫码枪可达（可选）
  ✓ 数据库可达（可选）

每项独立检查，前项失败不影响后续，最后输出 GO/STOP 总结。

用法::

    python tests/check_env.py
    python tests/check_env.py --skip-camera --skip-inference   # 只查软件包
"""

from __future__ import annotations

import argparse
import importlib
import socket
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_utils import setup_console_utf8
setup_console_utf8()


# ============================================================
# 打印工具
# ============================================================

_PASS, _FAIL, _WARN, _SKIP = "PASS", "FAIL", "WARN", "SKIP"

_results: list[tuple] = []


def check(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))
    icon = {"PASS": "[OK ]", "FAIL": "[!!!]",
            "WARN": "[ ? ]", "SKIP": "[ - ]"}[status]
    line = f"  {icon} {name}"
    if detail:
        line += f"  — {detail}"
    print(line)


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * (52 - len(title)))


# ============================================================
# 各项检查
# ============================================================

def check_python() -> None:
    section("Python 环境")
    py_ver = sys.version_info
    if py_ver < (3, 10):
        check("Python 版本", _FAIL,
              f"{py_ver.major}.{py_ver.minor}, 需要 ≥ 3.10")
    else:
        check("Python 版本", _PASS,
              f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}")


def check_packages() -> None:
    section("Python 包")
    required = [
        ("numpy",       "算法 + 相机帧拷贝"),
        ("cv2",         "图像处理 (opencv-python)"),
        ("PIL",         "图像 IO (Pillow)"),
    ]
    optional = [
        ("PyQt6",       "UI 框架（仅 main.py / main_camera.py 跑 UI 时需要）"),
        ("pymysql",     "MySQL 数据库（可选，未启用 DB 可跳过）"),
        ("requests",    "飞书 / MES HTTP（可选）"),
        ("serial",      "串口报警灯（可选，pyserial）"),
        ("matplotlib",  "stats_page 图表（可选）"),
        ("openpyxl",    "history_page 导出 Excel（可选）"),
        ("psutil",      "Run.bat 进程管理（可选）"),
    ]
    for mod, desc in required:
        try:
            importlib.import_module(mod)
            check(f"{mod}", _PASS, desc)
        except ImportError:
            check(f"{mod}", _FAIL, f"必需 — pip install ...; {desc}")
    for mod, desc in optional:
        try:
            importlib.import_module(mod)
            check(f"{mod}", _PASS, desc)
        except ImportError:
            check(f"{mod}", _WARN, f"可选 — {desc}")


def check_inference_dll() -> None:
    section("AI 推理 DLL")
    dll_dir = Path(r"D:\EasyLabel_x64\DeepLearning")
    if not dll_dir.is_dir():
        check("DLL 目录", _FAIL, f"{dll_dir} 不存在")
        return
    check("DLL 目录", _PASS, str(dll_dir))
    for fname in ("dnndefine.dll", "dnninfer.dll", "dnninfercpudll.dll"):
        if (dll_dir / fname).is_file():
            check(f"  {fname}", _PASS)
        else:
            sev = _FAIL if fname != "dnninfercpudll.dll" else _WARN
            check(f"  {fname}", sev, "缺失")


def check_models() -> None:
    section("模型文件")
    for fname in ("Model_seg.m", "Model_cls.m"):
        p = _REPO_ROOT / "models" / fname
        if p.is_file():
            size_mb = p.stat().st_size / 1024 / 1024
            check(f"  {p.relative_to(_REPO_ROOT)}", _PASS,
                  f"{size_mb:.1f} MB")
        else:
            check(f"  {p.relative_to(_REPO_ROOT)}", _FAIL, "不存在")


def check_bv_camera_install() -> None:
    section("BV 相机驱动")
    bv_root = Path(r"C:\Program Files\Bluevision\BVCam")
    if not bv_root.is_dir():
        check("BVCam 安装目录", _FAIL,
              f"{bv_root} 不存在 — 请装 BVCam 客户端")
        return
    check("BVCam 安装目录", _PASS, str(bv_root))

    dll = bv_root / "Driver" / "BVCam.dll"
    api_h = bv_root / "API" / "BVCamAPI.h"
    if dll.is_file():
        check("  Driver/BVCam.dll", _PASS)
    else:
        check("  Driver/BVCam.dll", _FAIL)
    if api_h.is_file():
        check("  API/BVCamAPI.h", _PASS, "头文件参考（非必需）")


def check_inference_runtime() -> None:
    section("推理运行时（加载 + 单次推理）")
    try:
        from sirod_inspector.algorithm import init_runtime
        init_runtime()
        check("init_runtime() DLL 加载", _PASS)
    except Exception as e:
        check("init_runtime() DLL 加载", _FAIL, str(e)[:80])
        return

    try:
        import cv2
        import numpy as np
        from sirod_inspector.algorithm import Segmenter

        seg_path = _REPO_ROOT / "models" / "Model_seg.m"
        if not seg_path.is_file():
            check("seg 模型推理", _SKIP, "Model_seg.m 不存在")
            return

        t0 = time.perf_counter()
        with Segmenter(str(seg_path)) as seg:
            init_ms = (time.perf_counter() - t0) * 1000
            test_img = np.full((1024, 3072), 128, dtype=np.uint8)
            t1 = time.perf_counter()
            r = seg.predict(test_img)
            infer_ms = (time.perf_counter() - t1) * 1000
            check("seg 模型推理", _PASS,
                  f"init={init_ms:.0f}ms  infer={infer_ms:.0f}ms  "
                  f"classes={seg.class_names}")
    except Exception as e:
        check("seg 模型推理", _FAIL, str(e)[:100])


def check_camera_enum(skip_grab: bool = False) -> None:
    section("BV 相机枚举 + 抓图")
    try:
        from sirod_inspector.camera import enumerate_devices, BVCamera, BVCameraError
    except ImportError as e:
        check("import camera module", _FAIL, str(e))
        return

    try:
        devs = enumerate_devices()
    except BVCameraError as e:
        check("enumerate_devices()", _FAIL, str(e)[:120])
        return
    if not devs:
        check("枚举到的设备数", _WARN,
              "0 台 — 检查相机供电/网线/防火墙/BVCam Viewer 是否占用")
        return
    check(f"枚举到的设备数", _PASS, f"{len(devs)} 台")
    for d in devs:
        check(f"  · {d.model}", _PASS,
              f"sn={d.serial}  ip={d.ip_addr}  uid=0x{d.uid:016X}")

    if skip_grab:
        check("抓图测试", _SKIP, "--skip-camera 参数")
        return

    try:
        with BVCamera() as cam:
            cam.configure(width=1024, height=15000,
                           trigger_source="Software", trigger_mode="On",
                           acquisition_mode="SingleFrame")
            cam.start()
            t0 = time.perf_counter()
            frame = cam.trigger_and_grab(timeout_ms=8000)
            dt = (time.perf_counter() - t0) * 1000
            cam.stop()
        check("单次软触发抓图", _PASS,
              f"{dt:.0f}ms  shape={frame.shape}  dtype={frame.dtype}  "
              f"min/max={frame.min()}/{frame.max()}")
    except Exception as e:
        check("单次软触发抓图", _FAIL, str(e)[:120])


def check_config() -> None:
    section("config.json")
    cfg_path = _REPO_ROOT / "sirod_inspector" / "config.json"
    if not cfg_path.is_file():
        check("config.json 存在", _WARN,
              "未生成 — 首次跑会自动创建默认值。"
              "或 cp config.example.json config.json")
        return
    check("config.json 存在", _PASS, str(cfg_path))

    try:
        import json
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        check("config.json 可解析", _FAIL, str(e))
        return
    check("config.json 可解析", _PASS, f"{len(cfg)} 个顶层键")

    # 推荐字段检查
    suggestions = [
        ("camera",   "main_camera.py 模式必需"),
        ("scanner",  "main_camera.py 模式需要"),
        ("judge",    "缺陷判定阈值"),
        ("models",   "可选 — 默认 models/Model_*.m"),
        ("alarm",    "报警开关"),
    ]
    for key, desc in suggestions:
        sev = _PASS if key in cfg else _WARN
        msg = f"已配置" if key in cfg else f"未配置（{desc}，会用默认值）"
        check(f"  {key}", sev, msg)


def check_scanner_reachable(cfg) -> None:
    section("扫码枪可达性（可选）")
    if not cfg.get("scanner", {}).get("enabled", False):
        check("扫码枪", _SKIP, "config 中未启用")
        return
    host = cfg.get("scanner", {}).get("host", "192.168.12.56")
    port = int(cfg.get("scanner", {}).get("port", 5000))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((host, port))
        s.close()
        check(f"TCP {host}:{port}", _PASS, "可连接")
    except Exception as e:
        check(f"TCP {host}:{port}", _WARN, f"不可达 ({e})")


def check_database_reachable(cfg) -> None:
    section("数据库可达性（可选）")
    db = cfg.get("database", {})
    host = db.get("host", "127.0.0.1")
    port = int(db.get("port", 3306))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((host, port))
        s.close()
        check(f"TCP {host}:{port}", _PASS, "可连接")
    except Exception as e:
        check(f"TCP {host}:{port}", _WARN, f"不可达 ({e})")


# ============================================================
# main
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="工厂机部署预检")
    ap.add_argument("--skip-camera", action="store_true",
                    help="跳过相机抓图测试（仅枚举）")
    ap.add_argument("--skip-inference", action="store_true",
                    help="跳过模型加载推理测试")
    args = ap.parse_args()

    print("=" * 60)
    print("SiRod Inspector — 部署预检")
    print("=" * 60)

    check_python()
    check_packages()
    check_inference_dll()
    check_models()
    check_bv_camera_install()
    if not args.skip_inference:
        check_inference_runtime()
    check_camera_enum(skip_grab=args.skip_camera)
    check_config()

    # 加载 config 后做网络检查
    cfg_path = _REPO_ROOT / "sirod_inspector" / "config.json"
    if cfg_path.is_file():
        try:
            import json
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            check_scanner_reachable(cfg)
            check_database_reachable(cfg)
        except Exception:
            pass

    # ──── 总结 ────
    print()
    print("=" * 60)
    counts = {_PASS: 0, _FAIL: 0, _WARN: 0, _SKIP: 0}
    for _, status, _ in _results:
        counts[status] += 1
    print(f"汇总: ✓ {counts[_PASS]} 项通过   "
          f"✗ {counts[_FAIL]} 项失败   "
          f"? {counts[_WARN]} 项警告   "
          f"- {counts[_SKIP]} 项跳过")

    if counts[_FAIL] > 0:
        print("\n[STOP] 有关键检查未通过，请先解决再部署：")
        for name, status, detail in _results:
            if status == _FAIL:
                print(f"  - {name}: {detail}")
        return 1
    elif counts[_WARN] > 0:
        print("\n[GO with caution] 没有阻塞错误，但有警告需确认：")
        for name, status, detail in _results:
            if status == _WARN:
                print(f"  - {name}: {detail}")
        return 0
    else:
        print("\n[GO] 全部检查通过 — 可执行 python sirod_inspector/main_camera.py")
        return 0


if __name__ == "__main__":
    sys.exit(main())
