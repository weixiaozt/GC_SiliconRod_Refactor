"""
BV-3110-GIGE 工业相机 ctypes 封装
==================================
基于 ``BVCam.dll`` (C ABI, ``BVCamAPI.h``)，提供高层 Python 接口。

调用流程::

    devs = enumerate_devices()                       # 枚举所有 BV 相机
    cam = BVCamera()                                  # 默认打开第一个
    cam.configure(width=1024, height=15000,
                   trigger_source="Software",
                   exposure_us=95.0)
    cam.start()
    frame = cam.trigger_and_grab(timeout_ms=5000)    # 软触发 + 阻塞获取
    # frame: np.ndarray, shape=(H, W), dtype=uint16 (Mono12)
    cam.stop()
    cam.close()

或用上下文管理器::

    with BVCamera() as cam:
        cam.configure(...)
        cam.start()
        frame = cam.trigger_and_grab()

依赖 DLL
--------
- ``C:\\Program Files\\Bluevision\\BVCam\\Driver\\BVCam.dll``
- ``C:\\Program Files\\Bluevision\\BVCam\\GenICam_v3_0\\bin\\Win64_x64\\*.dll``

可通过环境变量 ``BVCAM_DLL_DIR`` 覆盖 BVCam 安装根目录。
"""

from __future__ import annotations

import ctypes as C
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np

logger = logging.getLogger("SiRod.BVCamera")


# ============================================================
# 异常
# ============================================================

class BVCameraError(RuntimeError):
    """相机操作失败"""


# ============================================================
# DLL 加载
# ============================================================

_DEFAULT_BVCAM_ROOT = Path(r"C:\Program Files\Bluevision\BVCam")
_BVCAM_LIBRARY_STRUCT_VERSION = 30300

_dll: Optional[C.CDLL] = None
_dll_dir: Optional[Path] = None


def _load_dll() -> C.CDLL:
    """加载 BVCam.dll 并补全函数签名。幂等。"""
    global _dll, _dll_dir
    if _dll is not None:
        return _dll

    root = Path(os.environ.get("BVCAM_DLL_DIR", str(_DEFAULT_BVCAM_ROOT)))
    if not root.is_dir():
        raise BVCameraError(
            f"BVCam 安装目录不存在: {root}。"
            "请安装 Bluevision BVCam 客户端，"
            "或通过 BVCAM_DLL_DIR 环境变量指定路径。"
        )

    driver_dir = root / "Driver"
    genicam_dir = root / "GenICam_v3_0" / "bin" / "Win64_x64"
    api_dir64 = root / "API" / "x64"

    for d in (driver_dir, genicam_dir, api_dir64):
        if d.is_dir():
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass

    dll_path = driver_dir / "BVCam.dll"
    if not dll_path.is_file():
        raise BVCameraError(f"BVCam.dll 不存在: {dll_path}")

    try:
        dll = C.CDLL(str(dll_path))
    except OSError as e:
        raise BVCameraError(f"加载 BVCam.dll 失败: {e}") from e

    _bind_signatures(dll)

    # 库版本号必须先设置（否则结构体大小不匹配）
    if not dll.BVCAM_SetStructVersion(_BVCAM_LIBRARY_STRUCT_VERSION):
        logger.warning("BVCAM_SetStructVersion 返回 FALSE（继续）")

    _dll = dll
    _dll_dir = root
    logger.info(f"BVCam.dll 加载完成: {dll_path}")
    return dll


# ============================================================
# 结构体（与 BVCamAPI.h 对齐）
# ============================================================

# 枚举
BVCAM_GIGECAMERA = 1
BVCAM_USBCAMERA = 2

# OPENMODE
BVCAM_AUTO_ACCESS = 0
BVCAM_CONTROL_ACCESS = 2

# Status
STATUSIMAGE_COMPLETE = 0
STATUSIMAGE_REQUEST_PENDING = 1
STATUSIMAGE_REQUEST_ERROR = 2
STATUSIMAGE_REQUEST_RESTART = 3


class _BVCAM_GIGEDEVINFO(C.Structure):
    _pack_ = 4
    _fields_ = [
        ("Spec_Major",   C.c_uint16),
        ("Spec_Minor",   C.c_uint16),
        ("Dev_Mode",     C.c_uint32),
        ("IPConfig",     C.c_uint32),
        ("IPConfigOption", C.c_uint32),
        ("MACAddr",      C.c_uint8 * 6),
        ("IPAddr",       C.c_uint8 * 4),
        ("SubMask",      C.c_uint8 * 4),
        ("GateWay",      C.c_uint8 * 4),
        ("VendorName",   C.c_char * 32),
        ("Rsvd",         C.c_char),
        ("ModelName",    C.c_char * 32),
        ("Rsvd1",        C.c_char),
        ("Dev_Ver",      C.c_char * 32),
        ("Rsvd2",        C.c_char),
        ("Vendor_Info",  C.c_char * 48),
        ("Rsvd3",        C.c_char),
        ("SerialNumber", C.c_char * 16),
        ("Rsvd4",        C.c_char),
        ("UserName",     C.c_char * 16),
        ("Rsvd5",        C.c_uint32),
    ]


class _BVCAM_USBDEVINFO(C.Structure):
    _pack_ = 4
    _fields_ = [
        ("Dev_Capability", C.c_uint64),
        ("GenCP_Version",  C.c_uint32),
        ("VendorName",     C.c_char * 64),
        ("Rsvd",           C.c_char),
        ("ModelName",      C.c_char * 64),
        ("Rsvd1",          C.c_char),
        ("FamilyName",     C.c_char * 64),
        ("Rsvd2",          C.c_char),
        ("Dev_Version",    C.c_char * 64),
        ("Rsvd3",          C.c_char),
        ("Vendor_Info",    C.c_char * 64),
        ("Rsvd4",          C.c_char),
        ("SerialNumber",   C.c_char * 64),
        ("Rsvd5",          C.c_char),
        # USB 结构体后面字段头文件里未完整列出，但 BVCAM_DEVINFO 用 union 容纳，
        # 我们这里只用前面字段；额外字节由 union 总尺寸吃掉。
    ]


class _BVCAM_DEVINFO_UNION(C.Union):
    _fields_ = [("GigEDev", _BVCAM_GIGEDEVINFO),
                ("UsbDev",  _BVCAM_USBDEVINFO)]


class _BVCAM_DEVINFO(C.Structure):
    _pack_ = 4
    _fields_ = [
        ("UID",        C.c_uint64),
        ("BusNumber",  C.c_uint32),
        ("DeviceType", C.c_int32),
        ("Speed",      C.c_int32),
        ("u",          _BVCAM_DEVINFO_UNION),
    ]


def _make_list_struct(count: int):
    """构造容纳 N 个 BVCAM_DEVINFO 的 BVCAM_LIST 结构。
    BVCAM_LIST 是变长结构（Info[1] 是占位），需要按实际数量动态扩展。"""
    class _BVCAM_LIST(C.Structure):
        _pack_ = 4
        _fields_ = [("Count", C.c_uint32),
                    ("Info",  _BVCAM_DEVINFO * max(count, 1))]
    return _BVCAM_LIST


class _BVCAM_OPENPARAM(C.Structure):
    _pack_ = 4
    _fields_ = [
        ("DeviceType",   C.c_int32),
        ("UID",          C.c_uint64),
        ("IPAddr",       C.c_uint8 * 4),
        ("UserName",     C.c_char * 64),
        ("Rsvd",         C.c_uint32),
        ("AccMode",      C.c_int32),
        ("XML_DiskFlag", C.c_int32),
    ]


class _BVCAM_IMAGE(C.Structure):
    _pack_ = 4
    _fields_ = [
        ("Width",         C.c_uint32),
        ("Height",        C.c_uint32),
        ("pBuffer",       C.POINTER(C.c_uint8)),
        ("Length",        C.c_uint32),
        ("PixelFormatID", C.c_uint32),
        ("EndianMode",    C.c_int32),
        ("Status",        C.c_int32),
    ]


# ──────────────────────────────────────────────
# IMAGEFUNC ctypes 类型 + 模块级 image queue
# ──────────────────────────────────────────────
# C 头：VOID CALLBACK(HCAMERA, BVCAM_IMAGE*, BVCAM_IMAGEDATAINFO*, PVOID)
# Windows CALLBACK = __stdcall → WINFUNCTYPE
IMAGEFUNC = C.WINFUNCTYPE(
    None,
    C.c_void_p,                 # HCAMERA
    C.POINTER(_BVCAM_IMAGE),    # image
    C.c_void_p,                 # info（忽略）
    C.c_void_p,                 # context
)

# 模块级 queue + lock，避免 bound method ctypes 包装复杂性
# callback 极简：只把已 copy 的 bytes + shape 信息入队；主线程后处理 numpy
_image_queue: queue.Queue = queue.Queue()
_callback_call_count = 0
_callback_lock = threading.Lock()


def _module_image_callback(h_camera, image_ptr, info_ptr, context):
    """SDK 内部线程在每帧到达时调用。

    极简：只拷贝 raw bytes + 入队 (raw_bytes, width, height, pixel_format_id, length)
    主线程 trigger_and_grab 从 queue 拿到后再做 numpy reshape / dtype 解析。
    """
    global _callback_call_count
    try:
        with _callback_lock:
            _callback_call_count += 1
        if not image_ptr:
            return
        img = image_ptr.contents
        w = int(img.Width)
        h = int(img.Height)
        length = int(img.Length)
        pf_id = int(img.PixelFormatID)
        if not img.pBuffer or length == 0:
            return
        # 拷贝原始字节（最小操作，不做 reshape）
        # 用 ctypes.string_at 拷贝指定地址 length 字节，返回 Python bytes
        addr = C.cast(img.pBuffer, C.c_void_p).value
        raw = C.string_at(addr, length)
        _image_queue.put((raw, w, h, pf_id, length))
    except Exception:
        # 不能让异常传出 C 回调
        pass


# 必须保留 callback 对象引用（避免 GC）
_image_cb_ref = IMAGEFUNC(_module_image_callback)


# ============================================================
# 函数签名绑定
# ============================================================

def _bind_signatures(dll: C.CDLL) -> None:
    dll.BVCAM_SetStructVersion.argtypes = [C.c_uint32]
    dll.BVCAM_SetStructVersion.restype = C.c_int

    dll.BVCAM_GetList.argtypes = [C.c_void_p]    # 实际是 BVCAM_LIST*
    dll.BVCAM_GetList.restype = C.c_int

    dll.BVCAM_Open.argtypes = [C.POINTER(_BVCAM_OPENPARAM), C.POINTER(C.c_void_p)]
    dll.BVCAM_Open.restype = C.c_int

    dll.BVCAM_Close.argtypes = [C.c_void_p]
    dll.BVCAM_Close.restype = C.c_int

    dll.BVCAM_CameraInfo.argtypes = [C.c_void_p, C.POINTER(_BVCAM_DEVINFO)]
    dll.BVCAM_CameraInfo.restype = C.c_int

    dll.BVCAM_GetFeatureHandle.argtypes = [C.c_void_p, C.POINTER(C.c_void_p)]
    dll.BVCAM_GetFeatureHandle.restype = C.c_int

    dll.BVCAM_ResourceAlloc.argtypes = [C.c_void_p]
    dll.BVCAM_ResourceAlloc.restype = C.c_int

    dll.BVCAM_ResourceRelease.argtypes = [C.c_void_p]
    dll.BVCAM_ResourceRelease.restype = C.c_int

    dll.BVCAM_ImageStart.argtypes = [C.c_void_p]
    dll.BVCAM_ImageStart.restype = C.c_int

    dll.BVCAM_ImageStop.argtypes = [C.c_void_p]
    dll.BVCAM_ImageStop.restype = C.c_int

    # Callback 模式（BV Viewer 实测就用这个 — sync ImageReq/Complete 不工作）
    # 签名：BOOL BVCAM_SetImageCallBack(HCAMERA, PVOID, IMAGEFUNC, DWORD BufferCount, BOOL)
    dll.BVCAM_SetImageCallBack.argtypes = [
        C.c_void_p, C.c_void_p, IMAGEFUNC, C.c_uint32, C.c_int,
    ]
    dll.BVCAM_SetImageCallBack.restype = C.c_int

    dll.BVCAM_ImageAlloc.argtypes = [C.c_void_p, C.POINTER(C.POINTER(_BVCAM_IMAGE))]
    dll.BVCAM_ImageAlloc.restype = C.c_int

    dll.BVCAM_ImageFree.argtypes = [C.c_void_p, C.POINTER(_BVCAM_IMAGE)]
    dll.BVCAM_ImageFree.restype = C.c_int

    dll.BVCAM_ImageFreeAll.argtypes = [C.c_void_p]
    dll.BVCAM_ImageFreeAll.restype = C.c_int

    dll.BVCAM_ImageReq.argtypes = [C.c_void_p, C.POINTER(_BVCAM_IMAGE)]
    dll.BVCAM_ImageReq.restype = C.c_int

    dll.BVCAM_ImageComplete.argtypes = [
        C.c_void_p, C.POINTER(_BVCAM_IMAGE), C.c_uint32, C.c_void_p,
    ]
    dll.BVCAM_ImageComplete.restype = C.c_int

    dll.BVCAM_ImageReqAbortAll.argtypes = [C.c_void_p]
    dll.BVCAM_ImageReqAbortAll.restype = C.c_int

    # Feature
    dll.BVCAM_FeatureCommand.argtypes = [C.c_void_p, C.c_char_p]
    dll.BVCAM_FeatureCommand.restype = C.c_int

    dll.BVCAM_GetFeatureInteger.argtypes = [
        C.c_void_p, C.c_char_p, C.POINTER(C.c_int64), C.c_int,
    ]
    dll.BVCAM_GetFeatureInteger.restype = C.c_int

    dll.BVCAM_SetFeatureInteger.argtypes = [
        C.c_void_p, C.c_char_p, C.c_int64,
    ]
    dll.BVCAM_SetFeatureInteger.restype = C.c_int

    dll.BVCAM_GetFeatureFloat.argtypes = [
        C.c_void_p, C.c_char_p, C.POINTER(C.c_double), C.c_int,
    ]
    dll.BVCAM_GetFeatureFloat.restype = C.c_int

    dll.BVCAM_SetFeatureFloat.argtypes = [
        C.c_void_p, C.c_char_p, C.c_double,
    ]
    dll.BVCAM_SetFeatureFloat.restype = C.c_int

    dll.BVCAM_GetFeatureString.argtypes = [
        C.c_void_p, C.c_char_p, C.c_char_p, C.c_uint32, C.c_int,
    ]
    dll.BVCAM_GetFeatureString.restype = C.c_int

    dll.BVCAM_SetFeatureString.argtypes = [
        C.c_void_p, C.c_char_p, C.c_char_p,
    ]
    dll.BVCAM_SetFeatureString.restype = C.c_int

    dll.BVCAM_GetFeatureEnumeration.argtypes = [
        C.c_void_p, C.c_char_p, C.c_char_p, C.c_uint32, C.c_int,
    ]
    dll.BVCAM_GetFeatureEnumeration.restype = C.c_int

    dll.BVCAM_SetFeatureEnumeration.argtypes = [
        C.c_void_p, C.c_char_p, C.c_char_p,
    ]
    dll.BVCAM_SetFeatureEnumeration.restype = C.c_int

    dll.BVCAM_SetFeatureBoolean.argtypes = [
        C.c_void_p, C.c_char_p, C.c_int,
    ]
    dll.BVCAM_SetFeatureBoolean.restype = C.c_int

    # GetErrorMsg
    dll.BVCAM_GetErrorMsg.argtypes = [C.c_char_p, C.c_uint32]
    dll.BVCAM_GetErrorMsg.restype = None


def _last_error_msg(dll: C.CDLL) -> str:
    buf = (C.c_char * 512)()
    dll.BVCAM_GetErrorMsg(buf, 512)
    return buf.value.decode("ascii", errors="replace")


# ============================================================
# 像素格式 → numpy dtype 映射
# ============================================================
# GenICam Pixel Format ID（PFNC 标准）:
#   0x01080001  Mono8
#   0x01100003  Mono10
#   0x01100005  Mono12
#   0x01100007  Mono14
#   0x01100025  Mono16
# 高 16 位是布局信息，低 16 位是子格式。
# 简化：根据 (Length / Width / Height) 决定 dtype。

PIXEL_FORMAT_MAP = {
    0x01080001: ("Mono8",  np.uint8,  1),
    0x01100003: ("Mono10", np.uint16, 2),
    0x01100005: ("Mono12", np.uint16, 2),
    0x01100007: ("Mono14", np.uint16, 2),
    0x01100025: ("Mono16", np.uint16, 2),
}


def _decode_pixel_format(pf_id: int, length: int,
                        width: int, height: int) -> tuple:
    """根据 PixelFormatID + 缓冲长度推断 dtype 和每像素字节数。"""
    info = PIXEL_FORMAT_MAP.get(pf_id)
    if info:
        return info
    # 兜底：按 bytes / pixel 推断
    if width == 0 or height == 0:
        return ("Unknown", np.uint8, 1)
    bpp = length // (width * height)
    if bpp >= 2:
        return (f"Mono{bpp*8}", np.uint16, 2)
    return (f"Mono{bpp*8}", np.uint8, 1)


# ============================================================
# 设备描述
# ============================================================

@dataclass(frozen=True)
class BVCameraDevice:
    """枚举到的相机简要信息"""
    uid: int                        # 设备 UID（唯一标识）
    bus_number: int
    device_type: int                # 1=GigE, 2=USB
    vendor: str
    model: str
    serial: str
    ip_addr: str = ""               # GigE 才有

    @property
    def is_gige(self) -> bool:
        return self.device_type == BVCAM_GIGECAMERA

    def __str__(self) -> str:
        tag = "GigE" if self.is_gige else "USB"
        ip = f" ip={self.ip_addr}" if self.ip_addr else ""
        return (f"{tag} {self.vendor} {self.model} sn={self.serial}"
                f"{ip} uid=0x{self.uid:016X}")


# ============================================================
# 设备枚举
# ============================================================

_MAX_DEVICES_ENUMERATE = 32


def enumerate_devices() -> List[BVCameraDevice]:
    """枚举系统中所有可见的 BV 相机。

    BVCAM_GetList 的调用约定：
      - 调用方先准备一个能容纳 N 个 BVCAM_DEVINFO 的 buffer
      - 把 List->Count 设为 N (buffer 容量)
      - 调用后 List->Count 被改写为实际设备数
    """
    dll = _load_dll()

    full_cls = _make_list_struct(_MAX_DEVICES_ENUMERATE)
    full = full_cls()
    full.Count = _MAX_DEVICES_ENUMERATE
    if not dll.BVCAM_GetList(C.byref(full)):
        raise BVCameraError(f"BVCAM_GetList 失败: {_last_error_msg(dll)}")

    devs: List[BVCameraDevice] = []
    for i in range(full.Count):
        info = full.Info[i]
        if info.DeviceType == BVCAM_GIGECAMERA:
            g = info.u.GigEDev
            ip = ".".join(str(b) for b in g.IPAddr)
            devs.append(BVCameraDevice(
                uid=info.UID, bus_number=info.BusNumber,
                device_type=info.DeviceType,
                vendor=g.VendorName.decode("ascii", "replace"),
                model=g.ModelName.decode("ascii", "replace"),
                serial=g.SerialNumber.decode("ascii", "replace"),
                ip_addr=ip,
            ))
        elif info.DeviceType == BVCAM_USBCAMERA:
            u = info.u.UsbDev
            devs.append(BVCameraDevice(
                uid=info.UID, bus_number=info.BusNumber,
                device_type=info.DeviceType,
                vendor=u.VendorName.decode("ascii", "replace"),
                model=u.ModelName.decode("ascii", "replace"),
                serial=u.SerialNumber.decode("ascii", "replace"),
                ip_addr="",
            ))
    return devs


# ============================================================
# 主类
# ============================================================

class BVCamera:
    """单个 BV 相机的高层封装。

    生命周期::

        with BVCamera(uid=...) as cam:
            cam.configure(width=1024, height=15000,
                           trigger_source="Software")
            cam.start()
            for _ in range(N):
                frame = cam.trigger_and_grab(timeout_ms=5000)
                ...

    线程模型：单线程使用。多线程下需要外部加锁。
    """

    def __init__(self, uid: int = 0, *,
                 access_mode: int = BVCAM_AUTO_ACCESS):
        """
        Parameters
        ----------
        uid : int
            设备 UID。0 表示打开第一台可用相机。
        access_mode : int
            访问模式：``BVCAM_AUTO_ACCESS`` (0) / ``BVCAM_CONTROL_ACCESS`` (2)
        """
        self._dll = _load_dll()
        self._h_camera = C.c_void_p(0)
        self._h_feature = C.c_void_p(0)
        self._resources_allocated = False
        self._streaming = False
        self._image_alloc: Optional[C.POINTER(_BVCAM_IMAGE)] = None

        params = _BVCAM_OPENPARAM()
        params.DeviceType = 0           # ANYDEV
        params.UID = uid
        params.AccMode = access_mode
        params.XML_DiskFlag = 0

        # uid=0 时传 NULL 自动打开第一个
        param_ptr = C.byref(params) if uid else None

        if not self._dll.BVCAM_Open(param_ptr, C.byref(self._h_camera)):
            raise BVCameraError(f"打开相机失败: {_last_error_msg(self._dll)}")
        if not self._h_camera.value:
            raise BVCameraError("BVCAM_Open 返回 NULL 句柄")

        if not self._dll.BVCAM_GetFeatureHandle(
                self._h_camera, C.byref(self._h_feature)):
            self.close()
            raise BVCameraError(
                f"获取 FeatureHandle 失败: {_last_error_msg(self._dll)}"
            )

        # 读相机信息
        self.model = "?"
        self.serial = "?"
        self.vendor = "?"
        self.ip_addr = ""
        self.mac_addr = ""
        cinfo = _BVCAM_DEVINFO()
        if self._dll.BVCAM_CameraInfo(self._h_camera, C.byref(cinfo)):
            if cinfo.DeviceType == BVCAM_GIGECAMERA:
                g = cinfo.u.GigEDev
                self.model = g.ModelName.decode("ascii", "replace")
                self.serial = g.SerialNumber.decode("ascii", "replace")
                self.vendor = g.VendorName.decode("ascii", "replace")
                self.ip_addr = ".".join(str(b) for b in g.IPAddr)
                self.mac_addr = ":".join(f"{b:02X}" for b in g.MACAddr)
            else:
                u = cinfo.u.UsbDev
                self.model = u.ModelName.decode("ascii", "replace")
                self.serial = u.SerialNumber.decode("ascii", "replace")
                self.vendor = u.VendorName.decode("ascii", "replace")

        logger.info(f"相机已打开: {self.model} sn={self.serial}")

    # ─────────── 资源管理 ───────────

    def close(self) -> None:
        if not (self._h_camera and self._h_camera.value):
            return
        # 顺序：abort pending → stop stream → free image → release resource → close
        try:
            self._dll.BVCAM_ImageReqAbortAll(self._h_camera)
        except Exception:
            pass
        try:
            if self._streaming:
                self._dll.BVCAM_ImageStop(self._h_camera)
                self._streaming = False
        except Exception:
            pass
        try:
            if self._image_alloc is not None:
                self._dll.BVCAM_ImageFreeAll(self._h_camera)
                self._image_alloc = None
        except Exception:
            pass
        try:
            if self._resources_allocated:
                self._dll.BVCAM_ResourceRelease(self._h_camera)
                self._resources_allocated = False
        except Exception:
            pass
        try:
            self._dll.BVCAM_Close(self._h_camera)
        except Exception:
            pass
        self._h_camera = C.c_void_p(0)
        self._h_feature = C.c_void_p(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ─────────── Feature 读写 ───────────

    def set_int(self, name: str, value: int) -> None:
        if not self._dll.BVCAM_SetFeatureInteger(
                self._h_feature, name.encode("ascii"), C.c_int64(value)):
            raise BVCameraError(
                f"set_int({name}={value}) 失败: {_last_error_msg(self._dll)}"
            )

    def get_int(self, name: str, cache_clear: bool = False) -> int:
        v = C.c_int64(0)
        if not self._dll.BVCAM_GetFeatureInteger(
                self._h_feature, name.encode("ascii"),
                C.byref(v), 1 if cache_clear else 0):
            raise BVCameraError(
                f"get_int({name}) 失败: {_last_error_msg(self._dll)}"
            )
        return int(v.value)

    def set_float(self, name: str, value: float) -> None:
        if not self._dll.BVCAM_SetFeatureFloat(
                self._h_feature, name.encode("ascii"), C.c_double(value)):
            raise BVCameraError(
                f"set_float({name}={value}) 失败: {_last_error_msg(self._dll)}"
            )

    def get_float(self, name: str, cache_clear: bool = False) -> float:
        v = C.c_double(0)
        if not self._dll.BVCAM_GetFeatureFloat(
                self._h_feature, name.encode("ascii"),
                C.byref(v), 1 if cache_clear else 0):
            raise BVCameraError(
                f"get_float({name}) 失败: {_last_error_msg(self._dll)}"
            )
        return float(v.value)

    def set_string(self, name: str, value: str) -> None:
        """字符串型 feature 设置。"""
        if not self._dll.BVCAM_SetFeatureString(
                self._h_feature, name.encode("ascii"),
                value.encode("ascii")):
            raise BVCameraError(
                f"set_string({name}={value!r}) 失败: "
                f"{_last_error_msg(self._dll)}"
            )

    def get_string(self, name: str, cache_clear: bool = False) -> str:
        buf = (C.c_char * 256)()
        if not self._dll.BVCAM_GetFeatureString(
                self._h_feature, name.encode("ascii"),
                buf, 256, 1 if cache_clear else 0):
            raise BVCameraError(
                f"get_string({name}) 失败: {_last_error_msg(self._dll)}"
            )
        return buf.value.decode("ascii", errors="replace")

    def set_enum(self, name: str, value: str) -> None:
        """枚举型 feature 设置（AcquisitionMode/TriggerMode/TriggerSource 等）"""
        if not self._dll.BVCAM_SetFeatureEnumeration(
                self._h_feature, name.encode("ascii"),
                value.encode("ascii")):
            raise BVCameraError(
                f"set_enum({name}={value!r}) 失败: "
                f"{_last_error_msg(self._dll)}"
            )

    def get_enum(self, name: str, cache_clear: bool = False) -> str:
        buf = (C.c_char * 256)()
        if not self._dll.BVCAM_GetFeatureEnumeration(
                self._h_feature, name.encode("ascii"),
                buf, 256, 1 if cache_clear else 0):
            raise BVCameraError(
                f"get_enum({name}) 失败: {_last_error_msg(self._dll)}"
            )
        return buf.value.decode("ascii", errors="replace")

    def set_bool(self, name: str, value: bool) -> None:
        if not self._dll.BVCAM_SetFeatureBoolean(
                self._h_feature, name.encode("ascii"),
                1 if value else 0):
            raise BVCameraError(
                f"set_bool({name}={value}) 失败: "
                f"{_last_error_msg(self._dll)}"
            )

    def execute(self, name: str) -> None:
        """执行 command 型 feature（如 TriggerSoftware）"""
        if not self._dll.BVCAM_FeatureCommand(
                self._h_feature, name.encode("ascii")):
            raise BVCameraError(
                f"execute({name}) 失败: {_last_error_msg(self._dll)}"
            )

    # ─────────── 配置 ───────────

    def configure(self, *,
                  width: Optional[int] = None,
                  height: Optional[int] = None,
                  acquisition_mode: str = "SingleFrame",
                  acquisition_frame_count: Optional[int] = None,
                  trigger_mode: str = "On",
                  trigger_source: str = "Software",
                  exposure_us: Optional[float] = None) -> None:
        """一次性配置常用参数。``None`` 字段不动。

        Parameters
        ----------
        width, height : int
            图像尺寸。
            - SingleFrame 1024×15000：一次扫整张大图
            - MultiFrame  1024×100 + frame_count=150：扫 150 张小帧，外部拼回 15000 行
              （编码器同步对短帧更宽容，盐城现场用这种）
        acquisition_mode : str
            "SingleFrame" / "MultiFrame" / "Continuous"
        acquisition_frame_count : int | None
            ``MultiFrame`` 模式下相机一次触发要吐多少帧。``None`` / 不传则不动该字段。
            注意：调用方还要在 ``trigger_and_grab(frame_count=N)`` 里传相同的 N，
            才能完整收齐 N 帧拼图。
        trigger_mode : str
            "On" / "Off"
        trigger_source : str
            "Software" / "Line0" / "Line1" / "FrequencyConverter" …
        exposure_us : float
            曝光时间（微秒）。
        """
        # 设置顺序：先 trigger 关闭，调好尺寸再开 trigger，避免某些相机限制
        try:
            self.set_enum("AcquisitionMode", acquisition_mode)
        except BVCameraError as e:
            logger.warning(f"AcquisitionMode 设置失败（可能相机不支持）: {e}")

        # MultiFrame 模式下设置一次触发的帧数
        if acquisition_frame_count is not None:
            try:
                self.set_int("AcquisitionFrameCount", int(acquisition_frame_count))
            except BVCameraError as e:
                logger.warning(
                    f"AcquisitionFrameCount={acquisition_frame_count} 设置失败"
                    f"（相机可能不支持该 feature 或当前 mode 下不可写）: {e}"
                )

        if width is not None:
            self.set_int("Width", int(width))
        if height is not None:
            self.set_int("Height", int(height))

        # ★ 关键：先 set TriggerSelector 再 set TriggerMode/Source ★
        #
        # GenICam SFNC 规范：TriggerSelector 决定 TriggerMode/Source/Software
        # 作用在"哪种 trigger 事件"上。常见值：
        #   AcquisitionStart — 整个采集的开始（最通用，所有 mode 都能用）
        #   FrameStart       — 每一帧的开始（不是所有相机都支持）
        #   FrameBurstStart  — 一组帧的开始
        #
        # 实测 BV-C3110GE 不同 firmware 支持的 TriggerSelector 值不一样：
        #   sn=101067 (开发机)：支持 AcquisitionStart + FrameStart
        #   sn=101771 (盐城)：只支持 AcquisitionStart
        #
        # 所以统一用 AcquisitionStart —— SingleFrame + AcquisitionStart trigger
        # 一次触发吐 1 帧；MultiFrame + AcquisitionStart trigger 一次触发吐 N 帧。
        # 都符合 GenICam SFNC 规范。
        target_selector = "AcquisitionStart"
        try:
            self.set_enum("TriggerSelector", target_selector)
        except BVCameraError as e:
            logger.warning(
                f"TriggerSelector={target_selector} 设置失败（相机可能不支持）: {e}"
            )

        try:
            self.set_enum("TriggerMode", trigger_mode)
        except BVCameraError as e:
            logger.warning(f"TriggerMode 设置失败: {e}")
        try:
            self.set_enum("TriggerSource", trigger_source)
        except BVCameraError as e:
            logger.warning(f"TriggerSource 设置失败: {e}")

        if exposure_us is not None:
            try:
                self.set_float("ExposureTime", float(exposure_us))
            except BVCameraError as e:
                logger.warning(f"ExposureTime 设置失败: {e}")

        logger.info(
            f"相机配置: w={width} h={height} mode={acquisition_mode}"
            f"{f' fc={acquisition_frame_count}' if acquisition_frame_count else ''} "
            f"sel={target_selector} trigger={trigger_mode}/{trigger_source} "
            f"exp={exposure_us}us"
        )

    def read_all_params(self) -> dict:
        """从相机硬件实时读所有关键参数，返回 dict。

        失败的字段在 dict 里存为 ``None``（不抛异常，UI 用 ``None`` 显示 ``"<读取失败>"``）。
        包括：设备信息（model/serial/vendor/ip/mac）+ 帧参数 + 触发参数。
        不包含软件层参数（multiframe_first_wait_s / grab_timeout_ms）—— 那些由
        ``InspectEngineConfig`` 持有，由调用方合并。
        """
        def _safe(getter, name, kind):
            try:
                if kind == "int":
                    return self.get_int(name, cache_clear=True)
                if kind == "float":
                    return self.get_float(name, cache_clear=True)
                if kind == "enum":
                    return self.get_enum(name, cache_clear=True)
                return None
            except BVCameraError as e:
                logger.debug(f"读 {name} 失败（可能相机不支持）: {e}")
                return None

        return {
            # 设备只读
            "model":           self.model,
            "serial":          self.serial,
            "vendor":          self.vendor,
            "ip_addr":         self.ip_addr,
            "mac_addr":        self.mac_addr,
            # 帧参数
            "width":           _safe(self.get_int,   "Width",                 "int"),
            "height":          _safe(self.get_int,   "Height",                "int"),
            "acquisition_mode":         _safe(self.get_enum,  "AcquisitionMode",       "enum"),
            "acquisition_frame_count":  _safe(self.get_int,   "AcquisitionFrameCount", "int"),
            # 触发参数
            "trigger_selector":_safe(self.get_enum,  "TriggerSelector", "enum"),
            "trigger_mode":    _safe(self.get_enum,  "TriggerMode",   "enum"),
            "trigger_source":  _safe(self.get_enum,  "TriggerSource", "enum"),
            "exposure_us":     _safe(self.get_float, "ExposureTime",  "float"),
        }

    # ─────────── 流控 ───────────

    def start(self) -> None:
        """分配资源 + 注册 image callback + 启动采集流（callback 模式）。

        BV SDK 实测同步 ImageReq+Complete 拿不到 MultiFrame burst 的图，
        BV Viewer 用的也是 BVCAM_SetImageCallBack（strings dump 证实）。
        我们也走 callback：相机每帧到达 → SDK 内部线程调 _module_image_callback
        → 入队 → trigger_and_grab 主线程 queue.get。
        """
        if self._streaming:
            return
        if not self._resources_allocated:
            if not self._dll.BVCAM_ResourceAlloc(self._h_camera):
                raise BVCameraError(
                    f"ResourceAlloc 失败: {_last_error_msg(self._dll)}"
                )
            self._resources_allocated = True

        # 保留 ImageAlloc — 即使 callback 模式可能不用，留着不影响（向后兼容
        # apply_camera_params 等其他代码对 _image_alloc 的引用）
        if self._image_alloc is None:
            img_pp = C.POINTER(_BVCAM_IMAGE)()
            if not self._dll.BVCAM_ImageAlloc(
                    self._h_camera, C.byref(img_pp)):
                raise BVCameraError(
                    f"ImageAlloc 失败: {_last_error_msg(self._dll)}"
                )
            self._image_alloc = img_pp

        # 注册 callback —— 必须在 ImageStart 之前。BufferCount 给 8（小但够缓冲）
        # 用模块级 _image_cb_ref（已有引用，避免 GC）+ 模块级 _image_queue
        global _callback_call_count, _image_queue
        # 清空可能残留
        while not _image_queue.empty():
            try:
                _image_queue.get_nowait()
            except queue.Empty:
                break
        with _callback_lock:
            _callback_call_count = 0

        BUFFER_COUNT = 8
        t0 = time.perf_counter()
        if not self._dll.BVCAM_SetImageCallBack(
                self._h_camera, None, _image_cb_ref,
                C.c_uint32(BUFFER_COUNT), 0):
            raise BVCameraError(
                f"SetImageCallBack 失败: {_last_error_msg(self._dll)}"
            )
        logger.info(
            f"SetImageCallBack 注册成功 BufferCount={BUFFER_COUNT} "
            f"({(time.perf_counter()-t0)*1000:.0f}ms)"
        )

        if not self._dll.BVCAM_ImageStart(self._h_camera):
            raise BVCameraError(
                f"ImageStart 失败: {_last_error_msg(self._dll)}"
            )
        self._streaming = True
        logger.info("相机采集流已启动（callback 模式）")

    def ping(self) -> bool:
        """SDK 级探活：强制从设备读一个参数（cache_clear），读得到=相机还连着。

        用途：grab 因「没棒/编码器没转」超时时，没法靠帧流判断相机是否掉线。
        这里读一次设备寄存器——相机连着就能读到，掉线/断电则 SDK 调用失败。
        **必须由持有 _camera_lock 的线程（worker）调用**，不要从 UI 线程调
        （会和 20s 的 grab 抢锁卡住 UI）。读不到任何异常都吞掉返回 False。
        """
        if self._h_camera is None:
            return False
        try:
            self.get_int("Width", cache_clear=True)
            return True
        except Exception:
            return False

    def stop(self) -> None:
        if not self._streaming:
            return
        try:
            self._dll.BVCAM_ImageStop(self._h_camera)
            self._dll.BVCAM_ImageReqAbortAll(self._h_camera)
        finally:
            self._streaming = False
        logger.info("相机采集流已停止")

    # ─────────── 抓图 ───────────

    def _complete_one_frame(self, timeout_ms: int) -> np.ndarray:
        """已经发了 ImageReq + Trigger，等一帧完成并拷贝返回。

        失败时已经 abort pending request。调用方负责后续 Req。
        返回 ``(H, W)`` 的 numpy 数组（独立拷贝）。
        """
        rtn = self._dll.BVCAM_ImageComplete(
            self._h_camera, self._image_alloc, C.c_uint32(timeout_ms), None,
        )
        if not rtn:
            status = self._image_alloc.contents.Status
            try:
                self._dll.BVCAM_ImageReqAbortAll(self._h_camera)
            except Exception:
                pass
            raise BVCameraError(
                f"ImageComplete 失败 (status={status}): "
                f"{_last_error_msg(self._dll)}"
            )

        img = self._image_alloc.contents
        w, h, length = img.Width, img.Height, img.Length
        pf_id = img.PixelFormatID
        if not img.pBuffer or length == 0:
            raise BVCameraError("ImageComplete 成功但缓冲为空")

        name, dtype, bpp = _decode_pixel_format(pf_id, length, w, h)

        # 拷贝缓冲到 numpy（必须 copy，DLL 下次 ImageReq 复用底层缓冲）
        # 用 from_address + frombuffer + copy 替代 string_at —— 实测快 ~2x
        addr = C.addressof(img.pBuffer.contents)
        c_buf = (C.c_uint8 * length).from_address(addr)
        arr = np.frombuffer(c_buf, dtype=dtype).copy()
        # 处理可能的对齐填充（行末 padding）：理想情况 arr.size == h*w
        if arr.size == h * w:
            arr = arr.reshape(h, w)
        elif arr.size >= h * w:
            arr = arr[: h * w].reshape(h, w)
        else:
            logger.warning(
                f"图像缓冲大小不足: length={length}, "
                f"expected={h*w*bpp} (h={h} w={w} bpp={bpp})"
            )
            arr = arr.reshape(-1)

        logger.debug(
            f"单帧抓图: w={w} h={h} length={length} pf={name}({pf_id:#x}) "
            f"dtype={dtype.__name__}"
        )
        return arr

    def trigger_and_grab(self, timeout_ms: int = 5000,
                         frame_count: Optional[int] = None,
                         first_frame_wait_s: float = 0.0) -> np.ndarray:
        """发送软触发并阻塞拿帧。

        Parameters
        ----------
        timeout_ms : int
            单帧 ImageComplete 的超时（每帧独立计时）
        frame_count : int | None
            ``None`` 或 ``1``：SingleFrame 模式，触发一次拿一帧 ``(H, W)``。
            ``N > 1``：MultiFrame 模式 —— 触发后相机会连续吐 N 帧，本方法逐帧
            收齐后做 vertical concat，返回 ``(N*H, W)``。要求外部 ``configure()``
            时已经设了 ``acquisition_mode="MultiFrame"`` 和 ``acquisition_frame_count=N``。
        first_frame_wait_s : float
            仅 MultiFrame 模式生效：拿到第一帧后睡眠几秒再开始收后续帧，
            让编码器同步状态稳定。**对齐 Halcon Run.hdev:7184 wait_seconds(5)** ——
            原作者多年验证过的习惯，第一帧通常在编码器刚启动状态下不稳，
            等几秒让相机持续吐帧到 SDK 内部 buffer 队列里再消费，后续帧就稳。
            默认 ``0`` 不等待。

        返回
        ----
        np.ndarray
            形状 ``(H, W)`` 或 ``(N*H, W)``，dtype 取决于像素格式
            （Mono8→uint8, Mono10/12/14/16→uint16）。已 copy，外部安全持有。
        """
        if not self._streaming:
            raise BVCameraError("相机未启动采集流，先调用 start()")
        if self._image_alloc is None:
            raise BVCameraError("ImageAlloc 未完成")

        n = max(1, int(frame_count or 1))

        # 清空可能残留的帧（避免上次 trigger 的 stale 帧）
        global _image_queue, _callback_call_count
        before_n = _callback_call_count
        while not _image_queue.empty():
            try:
                _image_queue.get_nowait()
            except queue.Empty:
                break

        # 软触发整组 — 相机会通过编码器同步吐 N 帧，每帧 callback 入队
        t_trig = time.perf_counter()
        try:
            self.execute("TriggerSoftware")
        except BVCameraError:
            raise
        logger.debug(
            f"TriggerSoftware 发送 ({(time.perf_counter()-t_trig)*1000:.1f}ms)"
        )

        # 从 queue 收 N 帧（callback 在 SDK 内部线程入队）
        timeout_s = timeout_ms / 1000.0
        frames = []
        t_start = time.perf_counter()
        for i in range(n):
            try:
                raw, w, h, pf_id, length = _image_queue.get(timeout=timeout_s)
            except queue.Empty:
                with _callback_lock:
                    got = _callback_call_count - before_n
                raise BVCameraError(
                    f"queue.get 超时 ({timeout_ms}ms, 第 {i+1}/{n} 帧, "
                    f"callback 被调 {got} 次) — 编码器没转 / 相机没吐图 / "
                    f"callback 未注册成功"
                )

            # 解析 raw bytes 成 numpy 数组
            _, dtype, _ = _decode_pixel_format(pf_id, length, w, h)
            arr = np.frombuffer(raw, dtype=dtype)
            if arr.size == h * w:
                arr = arr.reshape(h, w)
            elif arr.size >= h * w:
                arr = arr[: h * w].reshape(h, w)
            else:
                logger.warning(
                    f"图像 buffer 不足: len={length} expected={h*w*dtype().itemsize}"
                )
            frames.append(arr)

            # 多帧模式首帧后强制等待（对齐 Halcon BV_GrapImage:7184）
            if i == 0 and n > 1 and first_frame_wait_s > 0:
                logger.debug(
                    f"MultiFrame 首帧后等待 {first_frame_wait_s}s"
                )
                time.sleep(first_frame_wait_s)

        if n == 1:
            return frames[0]

        stitched = np.vstack(frames)
        logger.debug(
            f"多帧拼接: {n} 帧 × {frames[0].shape} → {stitched.shape} "
            f"耗时 {(time.perf_counter()-t_start)*1000:.0f}ms"
        )
        return stitched

    # ─────────── 信息查询 ───────────

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def get_size(self) -> tuple:
        """返回 ``(width, height)``（来自当前相机配置）"""
        return self.get_int("Width", True), self.get_int("Height", True)
