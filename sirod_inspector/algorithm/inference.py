"""
深度学习推理 DLL Python 封装
==============================
通过 ctypes 调用本地 ``dnninfer.dll`` / ``dnndefine.dll`` 完成图像分类与分割推理。

高层 API
--------
    Classifier(model_path)            图像分类（top-1 + 全类别概率）
    Segmenter(model_path)             像素分割 + 连通块外接矩形
    init_runtime(dll_dir=None)        显式初始化运行时（可选）

调用示例::

    import cv2
    from sirod_inspector.algorithm.inference import Classifier, Segmenter

    img = cv2.imread("xxx.bmp", cv2.IMREAD_UNCHANGED)

    with Classifier("Model_cls.m") as cls:
        result = cls.predict(img)
        print(result.name, result.confidence)

    with Segmenter("Model_seg.m") as seg:
        result = seg.predict(img)
        for r in result.rects:
            print(r.name, r.left, r.top, r.width, r.height, r.area)

运行时约束
----------
- DLL 校验包含安装目录绑定：默认运行时目录为 ``D:\\EasyLabel_x64\\DeepLearning``。
  若部署机的安装位置不同，需通过 ``init_runtime(dll_dir=...)`` 指定，且仍需满足
  授权约束。
- 首次实例化 ``Classifier`` 或 ``Segmenter`` 时会自动调用 ``init_runtime()``。
- 初始化过程会切换进程 ``cwd`` 到 DLL 目录（DLL 自身要求），且**不可逆**。
  上层主程序应使用绝对路径处理资源文件，避免相对路径解析错位。
"""

from __future__ import annotations

import ctypes as C
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger("SiRod.Inference")


# ============================================================
# 运行时初始化（lazy）
# ============================================================

# 默认 DLL 目录。此路径由 SDK 安装包决定，DLL 内部带校验绑定。
_DEFAULT_RUNTIME_DIR = Path(r"D:\EasyLabel_x64\DeepLearning")

_runtime_lock = threading.Lock()
_runtime_dir: Optional[Path] = None
_dnndefine: Optional[C.CDLL] = None
_dnninfer: Optional[C.CDLL] = None


def init_runtime(dll_dir: str | Path | None = None) -> Path:
    """显式初始化推理 DLL 运行时。

    幂等：重复调用直接返回已绑定的目录（dll_dir 参数被忽略）。

    Parameters
    ----------
    dll_dir : str | Path | None
        DLL 所在目录。``None`` 时使用默认 ``D:\\EasyLabel_x64\\DeepLearning``。

    Returns
    -------
    Path
        实际生效的 DLL 目录。

    Raises
    ------
    FileNotFoundError
        DLL 目录或必需 DLL 文件不存在。
    RuntimeError
        DLL 加载或校验失败。

    Notes
    -----
    会切换进程 ``cwd`` 到 ``dll_dir``。这是 DLL 内部加载流程的硬性要求，
    无法绕过；之后无法恢复。
    """
    global _runtime_dir, _dnndefine, _dnninfer

    with _runtime_lock:
        if _runtime_dir is not None:
            return _runtime_dir

        target = Path(dll_dir) if dll_dir else _DEFAULT_RUNTIME_DIR
        if not target.is_dir():
            raise FileNotFoundError(
                f"深度学习推理 DLL 目录不存在: {target}。"
                "请确认相关运行时已安装，或调用 init_runtime(dll_dir=...) 指定位置。"
            )

        for required in ("dnndefine.dll", "dnninfer.dll"):
            if not (target / required).is_file():
                raise FileNotFoundError(
                    f"必需的 DLL 文件不存在: {target / required}"
                )

        logger.info(f"初始化推理运行时: {target}")
        os.chdir(target)
        for sub in (target, target.parent,
                    target / "Openvino",
                    target / "Libtorch",
                    target / "TensorRT"):
            if sub.is_dir():
                os.add_dll_directory(str(sub))

        try:
            dnndefine = C.CDLL(str(target / "dnndefine.dll"))
            dnninfer = C.CDLL(str(target / "dnninfer.dll"))
        except OSError as e:
            raise RuntimeError(f"加载推理 DLL 失败: {e}") from e

        _bind_signatures(dnndefine, dnninfer)
        dnninfer.DnnInfer_InitGlobalLog()

        _dnndefine = dnndefine
        _dnninfer = dnninfer
        _runtime_dir = target
        logger.info("推理运行时初始化完成")
        return target


def _ensure_runtime() -> None:
    """供内部 lazy 触发。已初始化则不动。"""
    if _runtime_dir is None:
        init_runtime()


# ============================================================
# C 结构体镜像（对应 SDK 头文件）
# ============================================================

class _Shape(C.Structure):
    _fields_ = [("N", C.c_uint), ("C", C.c_uint),
                ("H", C.c_uint), ("W", C.c_uint)]


class _Datum(C.Structure):
    _fields_ = [("shape", _Shape),
                ("data", C.c_void_p),
                ("data_item_size", C.c_int),
                ("alloc", C.c_int)]


class _SegRectResult(C.Structure):
    _fields_ = [
        ("image_id", C.c_int),
        ("label", C.c_int),
        ("rect_left", C.c_int),
        ("rect_top", C.c_int),
        ("rect_height", C.c_int),
        ("rect_width", C.c_int),
        ("rotatedrect_points", C.c_float * 8),
        ("rotatedrect_center", C.c_float * 2),
        ("rotatedrect_angle", C.c_float),
        ("contour_area", C.c_int),
        ("contour_arclength", C.c_int),
    ]


def _bind_signatures(dnndefine: C.CDLL, dnninfer: C.CDLL) -> None:
    """绑定 ctypes argtypes / restype。"""

    # ── dnndefine ──
    dnndefine.Shape_CreateV2.argtypes = [C.c_uint, C.c_uint, C.c_uint, C.c_uint]
    dnndefine.Shape_CreateV2.restype = _Shape

    dnndefine.Datum_CreateV2.argtypes = [C.c_int]
    dnndefine.Datum_CreateV2.restype = C.POINTER(_Datum)

    dnndefine.Datum_CreateV3.argtypes = [C.c_void_p, _Shape, C.c_int]
    dnndefine.Datum_CreateV3.restype = C.POINTER(_Datum)

    dnndefine.Datum_Destroy.argtypes = [C.POINTER(_Datum)]

    # ── dnninfer ──
    dnninfer.DnnInfer_InitGlobalLog.argtypes = []
    dnninfer.DnnInfer_UnInitGlobalLog.argtypes = []

    dnninfer.DnnInfer_IsDllValid.argtypes = [C.c_int, C.c_int]
    dnninfer.DnnInfer_IsDllValid.restype = C.c_bool

    dnninfer.DnnInfer_GetModelTypeFromModelFile.argtypes = [C.c_char_p]
    dnninfer.DnnInfer_GetModelTypeFromModelFile.restype = C.c_int

    dnninfer.DnnInfer_Init.argtypes = [C.c_char_p, C.c_int, C.c_int, C.c_int]
    dnninfer.DnnInfer_Init.restype = C.c_void_p

    dnninfer.DnnInfer_GetInputInfo.argtypes = [
        C.c_void_p,
        C.POINTER(C.c_int), C.POINTER(C.c_int),
        C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(C.c_int),
    ]
    dnninfer.DnnInfer_GetInputInfo.restype = C.c_bool

    dnninfer.DnnInfer_GetClassNum.argtypes = [C.c_void_p]
    dnninfer.DnnInfer_GetClassNum.restype = C.c_int

    dnninfer.DnnInfer_GetLabelName.argtypes = [C.c_void_p, C.c_char_p, C.c_int]
    dnninfer.DnnInfer_GetLabelName.restype = C.c_bool

    dnninfer.DnnInfer_GetErrorString.argtypes = [C.c_void_p]
    dnninfer.DnnInfer_GetErrorString.restype = C.c_char_p

    dnninfer.DnnInfer_Cls_Infer.argtypes = [
        C.c_void_p, C.POINTER(_Datum),
        C.POINTER(C.c_int), C.POINTER(C.c_float),
    ]
    dnninfer.DnnInfer_Cls_Infer.restype = C.c_bool

    dnninfer.DnnInfer_Seg_Infer.argtypes = [
        C.c_void_p, C.POINTER(_Datum), C.POINTER(_Datum),
    ]
    dnninfer.DnnInfer_Seg_Infer.restype = C.c_bool

    dnninfer.DnnInfer_Seg_Label2Rect.argtypes = [
        C.c_void_p, C.POINTER(_Datum),
        C.POINTER(_SegRectResult), C.c_int, C.POINTER(C.c_int),
    ]
    dnninfer.DnnInfer_Seg_Label2Rect.restype = C.c_int

    dnninfer.DnnInfer_Close.argtypes = [C.c_void_p]


# ============================================================
# 推理模式 / 结果数据类
# ============================================================

INFER_MODE_CPU = 0      # Intel CPU 后端
INFER_MODE_GPU = 1      # NVIDIA GPU 后端


@dataclass
class ClsResult:
    """分类推理结果"""
    label: int                          # top-1 标签索引
    name: str                           # top-1 类别名
    probs: List[float]                  # 全类别 softmax 概率

    @property
    def confidence(self) -> float:
        return self.probs[self.label]


@dataclass
class SegRect:
    """分割得到的单个连通块外接矩形"""
    label: int                          # 类别索引
    name: str                           # 类别名
    left: int                           # bbox 左上 x（原图坐标）
    top: int                            # bbox 左上 y
    width: int                          # bbox 宽
    height: int                         # bbox 高
    area: int                           # 轮廓像素面积
    arclength: int                      # 轮廓周长
    rotated_points: List[Tuple[float, float]] = field(default_factory=list)
    rotated_center: Tuple[float, float] = (0.0, 0.0)
    rotated_angle: float = 0.0


@dataclass
class SegResult:
    """分割推理结果"""
    label_map: np.ndarray               # (H, W) uint8 类别标签图
    rects: List[SegRect]                # 连通块外接矩形列表
    class_names: List[str]              # 模型支持的所有类别名


# ============================================================
# 基类：模型加载 / 输入信息查询 / 释放
# ============================================================

class _BaseInfer:
    """共享 DLL 调用：Init / GetInputInfo / GetLabelName / Close"""

    def __init__(self, model_path: str | Path, *,
                 infer_mode: int = INFER_MODE_CPU,
                 device_index: int = 0,
                 batch_size: int = 1):
        # 先把相对路径基于当前 cwd 解析为绝对路径，再触发 runtime 初始化
        # （_ensure_runtime 会 chdir 到 DLL 目录，否则相对路径会解析错位置）
        self._model_path = Path(model_path).resolve()
        if not self._model_path.exists():
            raise FileNotFoundError(self._model_path)

        _ensure_runtime()

        # 文件类型 0 = .m 模型；DLL 校验当前后端是否可用
        if not _dnninfer.DnnInfer_IsDllValid(0, infer_mode):
            raise RuntimeError(
                f"推理 DLL 不支持 infer_mode={infer_mode}。"
                "请确认授权与安装位置正确。"
            )

        # DLL 内部用 GBK 解析模型路径
        self._handle = _dnninfer.DnnInfer_Init(
            str(self._model_path).encode("gbk"),
            batch_size, infer_mode, device_index,
        )
        if not self._handle:
            raise RuntimeError(f"DnnInfer_Init 返回 NULL: {self._model_path}")

        # 读模型输入元数据
        w, h, ch, bs, cls = (C.c_int(0) for _ in range(5))
        _dnninfer.DnnInfer_GetInputInfo(
            self._handle, C.byref(w), C.byref(h), C.byref(ch),
            C.byref(bs), C.byref(cls),
        )
        self.input_width = w.value
        self.input_height = h.value
        self.input_channels = ch.value
        self.batch_size = bs.value
        self.num_classes = cls.value

        # 读类别名
        self.class_names: List[str] = []
        for i in range(self.num_classes):
            buf = C.create_string_buffer(64)
            _dnninfer.DnnInfer_GetLabelName(self._handle, buf, i)
            self.class_names.append(buf.value.decode("gbk", errors="replace"))

        logger.info(
            f"模型已加载: {self._model_path.name} "
            f"input={self.input_width}x{self.input_height}x{self.input_channels} "
            f"classes={self.class_names}"
        )

    def close(self) -> None:
        if getattr(self, "_handle", None):
            _dnninfer.DnnInfer_Close(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ────── 输入数据包装 ──────
    def _make_input_datum(self, img: np.ndarray):
        """把 numpy 数组零拷贝包装成 Datum*。

        调用方需在推理完成前保持对 ``img`` 的引用（防 GC 回收底层内存）。
        """
        if not img.flags["C_CONTIGUOUS"]:
            img = np.ascontiguousarray(img)
        channels = 1 if img.ndim == 2 else img.shape[2]
        rows, cols = img.shape[0], img.shape[1]
        shape = _dnndefine.Shape_CreateV2(1, channels, rows, cols)
        datum_p = _dnndefine.Datum_CreateV3(img.ctypes.data, shape, 1)
        return datum_p, img

    def _last_error(self) -> str:
        err = _dnninfer.DnnInfer_GetErrorString(self._handle)
        return err.decode("gbk", errors="replace") if err else ""


# ============================================================
# 分类器
# ============================================================

class Classifier(_BaseInfer):
    """图像分类。输出 top-1 label + 全类别 softmax 概率。"""

    def __init__(self, model_path, **kwargs):
        super().__init__(model_path, **kwargs)
        # 复用输出缓冲
        self._label_buf = (C.c_int * 1024)()
        self._prob_buf = (C.c_float * 1024)()

    def predict(self, img: np.ndarray) -> ClsResult:
        datum_p, _img_ref = self._make_input_datum(img)
        try:
            ok = _dnninfer.DnnInfer_Cls_Infer(
                self._handle, datum_p, self._label_buf, self._prob_buf,
            )
            if not ok:
                raise RuntimeError(f"分类推理失败: {self._last_error()}")
            top = int(self._label_buf[0])
            probs = [float(self._prob_buf[i]) for i in range(self.num_classes)]
            name = self.class_names[top] if top < len(self.class_names) else str(top)
            return ClsResult(label=top, name=name, probs=probs)
        finally:
            _dnndefine.Datum_Destroy(datum_p)


# ============================================================
# 分割器
# ============================================================

_MAX_SEG_OBJECTS = 512


class Segmenter(_BaseInfer):
    """像素分割 + 连通块外接矩形提取。"""

    def __init__(self, model_path, **kwargs):
        super().__init__(model_path, **kwargs)
        # 复用输出缓冲：label map (uint8) + bbox 数组
        self._out_label = _dnndefine.Datum_CreateV2(1)
        self._rects_buf = (_SegRectResult * _MAX_SEG_OBJECTS)()

    def close(self) -> None:
        if getattr(self, "_out_label", None):
            _dnndefine.Datum_Destroy(self._out_label)
            self._out_label = None
        super().close()

    def predict(self, img: np.ndarray) -> SegResult:
        datum_p, _img_ref = self._make_input_datum(img)
        try:
            ok = _dnninfer.DnnInfer_Seg_Infer(
                self._handle, datum_p, self._out_label,
            )
            if not ok:
                raise RuntimeError(f"分割推理失败: {self._last_error()}")

            # 拷贝 label map（DLL 内部缓冲会被下次调用覆盖）
            out_shape = self._out_label.contents.shape
            oH, oW = out_shape.H, out_shape.W
            buf_size = out_shape.N * out_shape.C * oH * oW
            label_map = np.ctypeslib.as_array(
                (C.c_uint8 * buf_size).from_address(self._out_label.contents.data)
            ).reshape(oH, oW).copy()

            # 转外接矩形
            used = C.c_int(0)
            _dnninfer.DnnInfer_Seg_Label2Rect(
                self._handle, self._out_label,
                self._rects_buf, _MAX_SEG_OBJECTS, C.byref(used),
            )

            rects: List[SegRect] = []
            for i in range(used.value):
                r = self._rects_buf[i]
                pts = [(float(r.rotatedrect_points[2 * k]),
                        float(r.rotatedrect_points[2 * k + 1])) for k in range(4)]
                rects.append(SegRect(
                    label=int(r.label),
                    name=(self.class_names[r.label]
                          if r.label < len(self.class_names) else str(r.label)),
                    left=int(r.rect_left),
                    top=int(r.rect_top),
                    width=int(r.rect_width),
                    height=int(r.rect_height),
                    area=int(r.contour_area),
                    arclength=int(r.contour_arclength),
                    rotated_points=pts,
                    rotated_center=(float(r.rotatedrect_center[0]),
                                    float(r.rotatedrect_center[1])),
                    rotated_angle=float(r.rotatedrect_angle),
                ))

            return SegResult(
                label_map=label_map,
                rects=rects,
                class_names=list(self.class_names),
            )
        finally:
            _dnndefine.Datum_Destroy(datum_p)
