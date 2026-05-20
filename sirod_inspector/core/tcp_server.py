"""
TCP 通信服务器模块 — HALCON 协议版
====================================
通过 mvtec-halcon Python 包接收 Halcon 发送的图像和数据元组。

Halcon 端发送顺序：
  1. send_image(ObjectData, Socket)   — 先发送检测图像
  2. send_tuple(Socket, [JsonString]) — 再发送 JSON 元数据字符串

JSON 元数据字段（中文 key）：
    {
        "ID": 123456,         // 检测 ID（整数）
        "晶编": "0000",       // 晶棒编号（字符串）
        "质量": 0,            // 0=OK, 非0=NG
        "个数": 0,            // 缺陷个数
        "最大面积": 0,        // 最大缺陷面积
        "总面积": 0,          // 缺陷总面积
        "最大长度": 0          // 最大缺陷长度
    }

Halcon 端连接示例：
    open_socket_connect('127.0.0.1', 3000,
                        ['protocol','timeout'], ['HALCON',1], Socket)
    send_image(ObjectData, Socket)
    send_tuple(Socket, [JsonString])

依赖：
    pip install mvtec-halcon
    （需要本机安装 HALCON 运行时许可）
"""

import ctypes
import json
import logging
import threading
import time
import traceback
import datetime
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 尝试导入 halcon 包 ──
try:
    import halcon as ha
    HAS_HALCON = True
    logger.info("mvtec-halcon 包已加载")
except Exception as _halcon_import_err:
    HAS_HALCON = False
    ha = None
    logger.warning(
        f"HALCON 不可用（原因: {_halcon_import_err}）。"
        "TCP 服务器将无法启动，程序其他功能不受影响。"
    )

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ─────────────────────────────────────────────
#  数据结构
# ─────────────────────────────────────────────
@dataclass
class InspectData:
    """一次检测数据"""
    rod_id: str = ""                        # 晶棒编号（来自 JSON "晶编"）
    result: str = "OK"                      # "OK" 或 "NG"
    image: Optional[np.ndarray] = None      # HxW 或 HxWx3 numpy 数组
    defect_type: str = ""                   # 缺陷类型（来自 JSON "类型"，如 "隐裂"、"崩边"）
    defect_count: int = 0                   # 缺陷个数（来自 JSON "个数"）
    max_area: float = 0.0                   # 最大缺陷面积（像素²，内部/DB 口径）
    total_area: float = 0.0                 # 缺陷总面积（像素²）
    max_length: float = 0.0                 # 最大缺陷长度（像素，= outer_radius 半径，内部/DB 口径）
    # ↓ 显示/上报层换算值（mm）。长度按「直径」算 = 2×outer_radius / ppm；面积 = px / ppm²。
    #   ppm 未标定（<=0）时统一为 0.0。内部判定/DB 仍用上面的像素口径。
    max_length_mm: float = 0.0              # 最大缺陷长度（mm，直径口径，给人看/上报 MES）
    max_area_mm2: float = 0.0               # 最大缺陷面积（mm²）
    total_area_mm2: float = 0.0             # 缺陷总面积（mm²）
    inspect_id: int = 0                     # 检测 ID
    quality: int = 0                        # 质量（0=OK, 1=NG，来自 JSON "质量"）
    ct: float = 0.0                         # 检测时长（毫秒，来自 JSON "检测时长"）
    check_time: str = ""                    # 检测时间（Halcon 原始格式，来自 JSON "检测时间"）
    upload_time: str = ""                   # 数据上传时间（Python 端自动生成）
    timestamp: str = ""                     # 检测时间（格式化后）
    raw_json: dict = field(default_factory=dict)


# ─────────────────────────────────────────────
#  HALCON 图像类型 → numpy dtype 映射
# ─────────────────────────────────────────────
_HALCON_TYPE_MAP = {
    "byte":      (ctypes.c_uint8,  np.uint8),
    "int1":      (ctypes.c_int8,   np.int8),
    "int2":      (ctypes.c_int16,  np.int16),
    "uint2":     (ctypes.c_uint16, np.uint16),
    "int4":      (ctypes.c_int32,  np.int32),
    "int8":      (ctypes.c_int64,  np.int64),
    "real":      (ctypes.c_double, np.float64),
    "direction": (ctypes.c_uint8,  np.uint8),
    "cyclic":    (ctypes.c_uint8,  np.uint8),
}


def _himage_to_numpy(himage) -> Optional[np.ndarray]:
    """
    将 HALCON HImage (HObject) 转换为 numpy 数组。

    使用 ha.get_image_pointer1 获取图像指针、类型、宽高，
    然后通过 ctypes 读取像素数据并拷贝为 numpy 数组。

    对于多通道图像（如 RGB），使用 ha.count_channels 检测通道数，
    并分别读取每个通道后合并。
    """
    if not HAS_HALCON or himage is None:
        return None

    try:
        # 获取通道数
        num_channels = _unwrap(ha.count_channels(himage))
        num_channels = int(num_channels)

        if num_channels == 1:
            # 单通道图像
            pointer, img_type, width, height = ha.get_image_pointer1(himage)
            return _read_channel(pointer, img_type, width, height)

        elif num_channels == 3:
            # 三通道图像 — 使用 get_image_pointer3
            try:
                pr, pg, pb, img_type, width, height = ha.get_image_pointer3(himage)
                ch_r = _read_channel(pr, img_type, width, height)
                ch_g = _read_channel(pg, img_type, width, height)
                ch_b = _read_channel(pb, img_type, width, height)
                if ch_r is not None and ch_g is not None and ch_b is not None:
                    return np.stack([ch_r, ch_g, ch_b], axis=-1)
            except Exception:
                # 回退：逐通道 access_channel + get_image_pointer1
                channels = []
                for ch_idx in range(1, num_channels + 1):
                    ch_img = ha.access_channel(himage, ch_idx)
                    pointer, img_type, width, height = ha.get_image_pointer1(ch_img)
                    ch_arr = _read_channel(pointer, img_type, width, height)
                    if ch_arr is not None:
                        channels.append(ch_arr)
                if len(channels) == num_channels:
                    return np.stack(channels, axis=-1)

        else:
            # 其他通道数：只取第一通道
            pointer, img_type, width, height = ha.get_image_pointer1(himage)
            return _read_channel(pointer, img_type, width, height)

    except Exception as e:
        logger.error(f"HImage 转 numpy 失败: {e}", exc_info=True)

    return None


def _unwrap(val):
    """
    HALCON Python API 有时返回单元素列表/元组而非标量，
    例如 get_image_pointer1 返回 (ptr, ['byte'], [640], [480])。
    此函数将其解包为标量值。
    """
    while isinstance(val, (list, tuple)):
        if len(val) == 1:
            val = val[0]
        else:
            break
    return val


def _read_channel(pointer, img_type, width, height) -> Optional[np.ndarray]:
    """从内存指针读取单通道图像数据为 numpy 数组（拷贝）"""
    try:
        # 解包可能的列表/元组返回值
        pointer = _unwrap(pointer)
        img_type = _unwrap(img_type)
        width = _unwrap(width)
        height = _unwrap(height)

        img_type_lower = str(img_type).lower().strip("'\"")
        if img_type_lower not in _HALCON_TYPE_MAP:
            logger.warning(f"不支持的 HALCON 图像类型: {img_type} (解析后: {img_type_lower})")
            return None

        c_type, np_dtype = _HALCON_TYPE_MAP[img_type_lower]
        num_pixels = int(width) * int(height)
        ptr_int = int(pointer)

        # 通过 ctypes 从指针读取像素数据
        arr_type = c_type * num_pixels
        c_arr = arr_type.from_address(ptr_int)
        np_arr = np.ctypeslib.as_array(c_arr).reshape((int(height), int(width)))

        # 拷贝数据，避免 HALCON 释放内存后悬空指针
        return np_arr.copy()

    except Exception as e:
        logger.error(f"读取图像通道失败: {e}", exc_info=True)
        return None


# ─────────────────────────────────────────────
#  TCPServer — HALCON 协议版
# ─────────────────────────────────────────────
class TCPServer:
    """
    HALCON 协议 TCP 服务器。

    使用 mvtec-halcon 包创建 HALCON 协议 Socket 服务器，
    监听指定端口，接收 Halcon 客户端发送的图像和数据元组。

    使用方式：
        server = TCPServer(host="127.0.0.1", port=3000)
        server.register_callback(on_data)
        server.start()
        ...
        server.stop()
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 3000):
        self._host = host
        self._port = port
        self._accepting_socket = None
        self._running = False
        self._accept_thread: Optional[threading.Thread] = None
        self._client_threads: list = []
        self._callbacks: list = []
        self._lock = threading.Lock()

        # 统计
        self._total_received = 0
        self._connected_clients = 0

    # ─────────── 属性 ───────────
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def total_received(self) -> int:
        return self._total_received

    @property
    def connected_clients(self) -> int:
        return self._connected_clients

    # ─────────── 回调管理 ───────────
    def register_callback(self, callback: Callable[[InspectData], None]):
        """注册数据到达回调函数"""
        self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable):
        """注销回调"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    # ─────────── 启动 / 停止 ───────────
    def start(self):
        """启动 HALCON 协议 TCP 服务器"""
        if self._running:
            logger.warning("TCP 服务器已在运行")
            return

        if not HAS_HALCON:
            raise RuntimeError(
                "mvtec-halcon 包未安装或 HALCON 运行时不可用。"
                "请执行: pip install mvtec-halcon 并确保已安装 HALCON 运行时。"
            )

        try:
            # 创建 HALCON 接受连接的 Socket
            # open_socket_accept(Port, GenParamName, GenParamValue) -> AcceptingSocket
            # 使用 'HALCON4' 强制 IPv4，避免 IPv6 监听导致 IPv4 客户端连接失败
            self._accepting_socket = ha.open_socket_accept(
                self._port,
                ['protocol', 'timeout', 'address'],
                ['HALCON4', 5.0, self._host]
            )
            self._running = True

            self._accept_thread = threading.Thread(
                target=self._accept_loop, daemon=True, name="HALCON-Accept"
            )
            self._accept_thread.start()

            logger.info(
                f"HALCON 协议 TCP 服务器已启动: {self._host}:{self._port}"
            )
        except Exception as e:
            logger.error(f"TCP 服务器启动失败: {e}", exc_info=True)
            self._running = False
            raise

    def stop(self):
        """停止 TCP 服务器"""
        self._running = False

        # 关闭接受连接的 Socket
        if self._accepting_socket is not None:
            try:
                ha.close_socket(self._accepting_socket)
            except Exception:
                pass
            self._accepting_socket = None

        if self._accept_thread:
            self._accept_thread.join(timeout=5)

        for t in self._client_threads:
            t.join(timeout=3)
        self._client_threads.clear()

        logger.info("HALCON 协议 TCP 服务器已停止")

    def update_address(self, host: str, port: int):
        """更新服务器地址（需要先 stop 再 start 才生效）"""
        self._host = host
        self._port = port
        logger.info(f"TCP 服务器地址已更新为: {host}:{port}")

    # ─────────── 接受连接循环 ───────────
    def _accept_loop(self):
        """
        后台线程：循环接受 Halcon 客户端连接。
        使用 socket_accept_connect 等待并接受连接。
        """
        logger.info("HALCON 接受连接线程已启动")

        while self._running:
            try:
                # socket_accept_connect(AcceptingSocket, Wait) -> Socket
                # Wait='auto' 使用 open_socket_accept 设置的 timeout
                client_socket = ha.socket_accept_connect(
                    self._accepting_socket, 'auto'
                )

                with self._lock:
                    self._connected_clients += 1

                logger.info(
                    f"Halcon 客户端已连接 "
                    f"(当前连接数: {self._connected_clients})"
                )

                # 为每个连接启动独立的处理线程
                t = threading.Thread(
                    target=self._client_handler,
                    args=(client_socket,),
                    daemon=True,
                    name=f"HALCON-Client-{self._connected_clients}"
                )
                t.start()
                self._client_threads.append(t)

                # 清理已结束的线程
                self._client_threads = [
                    t for t in self._client_threads if t.is_alive()
                ]

            except Exception as e:
                if self._running:
                    err_str = str(e).lower()
                    # 超时是正常的，继续等待
                    if "timeout" in err_str or "9400" in str(e):
                        continue
                    logger.error(
                        f"接受连接异常: {e}", exc_info=True
                    )
                    time.sleep(0.5)
                else:
                    break

        logger.info("HALCON 接受连接线程已退出")

    # ─────────── 客户端处理 ───────────
    def _client_handler(self, client_socket):
        """
        处理单个 Halcon 客户端连接。

        接收顺序（循环）：
          1. receive_image  — 接收检测图像
          2. receive_tuple  — 接收 JSON 元数据字符串
        """
        try:
            while self._running:
                try:
                    # ── 步骤 1：接收图像 ──
                    logger.debug("等待接收 HALCON 图像...")
                    himage = ha.receive_image(client_socket)
                    logger.debug("已接收 HALCON 图像")

                    # 转换为 numpy 数组
                    np_image = _himage_to_numpy(himage)
                    if np_image is not None:
                        logger.debug(
                            f"图像转换成功: shape={np_image.shape}, "
                            f"dtype={np_image.dtype}"
                        )
                    else:
                        logger.warning("图像转换为 numpy 失败")

                    # ── 步骤 2：接收元数据元组 ──
                    logger.debug("等待接收 HALCON 元组...")
                    tuple_data = ha.receive_tuple(client_socket)
                    logger.debug(f"已接收元组: {tuple_data}")

                    # 解析元组中的 JSON 字符串
                    metadata = self._parse_tuple(tuple_data)

                    # 组装 InspectData
                    inspect_data = self._build_inspect_data(metadata, np_image)
                    self._total_received += 1

                    logger.info(
                        f"收到检测数据 #{self._total_received}: "
                        f"ID={inspect_data.inspect_id}, "
                        f"rod_id={inspect_data.rod_id}, "
                        f"result={inspect_data.result}, "
                        f"defect_count={inspect_data.defect_count}"
                    )

                    # 分发到回调
                    self._dispatch(inspect_data)

                except Exception as e:
                    if not self._running:
                        break
                    err_str = str(e).lower()
                    # 连接断开
                    if any(kw in err_str for kw in
                           ["socket", "connection", "closed", "reset",
                            "broken", "eof", "9400"]):
                        logger.info("Halcon 客户端连接已断开")
                        break
                    logger.error(
                        f"接收数据异常: {e}", exc_info=True
                    )
                    break

        finally:
            # 关闭客户端 Socket
            try:
                ha.close_socket(client_socket)
            except Exception:
                pass

            with self._lock:
                self._connected_clients = max(0, self._connected_clients - 1)

            logger.info(
                f"Halcon 客户端处理线程退出 "
                f"(剩余连接数: {self._connected_clients})"
            )

    # ─────────── 数据解析 ───────────
    @staticmethod
    def _parse_tuple(tuple_data) -> dict:
        """
        解析 Halcon send_tuple 发送的元组数据。

        Halcon 端将 JSON 字符串作为元组的第一个元素发送：
            send_tuple(Socket, [JsonString])

        元组格式：(json_string,) 或 [json_string]
        JSON 字段：{"ID":123456, "晶编":"0000", "质量":0, "个数":0,
                    "最大面积":0, "总面积":0, "最大长度":0}
        """
        metadata = {}

        try:
            if tuple_data is None:
                logger.warning("接收到空元组")
                return metadata

            # tuple_data 可能是 tuple、list 或单个值
            raw = tuple_data
            if isinstance(raw, (tuple, list)):
                if len(raw) == 0:
                    logger.warning("接收到空元组列表")
                    return metadata
                # 取第一个元素（JSON 字符串）
                raw = raw[0]

            # 转为字符串
            json_str = str(raw).strip()

            # 尝试 JSON 解析
            if json_str.startswith("{"):
                metadata = json.loads(json_str)
                logger.debug(f"JSON 解析成功: {metadata}")
            else:
                # 可能不是 JSON 格式，尝试直接解析
                logger.warning(
                    f"元组数据不是 JSON 格式: {json_str[:200]}"
                )
                # 尝试整个 tuple_data 作为多字段解析
                if isinstance(tuple_data, (tuple, list)) and len(tuple_data) >= 7:
                    metadata = {
                        "ID": tuple_data[0],
                        "晶编": str(tuple_data[1]),
                        "质量": tuple_data[2],
                        "个数": tuple_data[3],
                        "最大面积": tuple_data[4],
                        "总面积": tuple_data[5],
                        "最大长度": tuple_data[6],
                    }
                    logger.info("已按位置解析元组字段")

        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}, 原始数据: {str(tuple_data)[:300]}")
        except Exception as e:
            logger.error(f"解析元组异常: {e}", exc_info=True)

        return metadata

    @staticmethod
    def _build_inspect_data(metadata: dict, image: Optional[np.ndarray]) -> InspectData:
        """
        根据解析后的元数据和图像构建 InspectData 对象。

        JSON 字段映射：
            "ID"      → inspect_id
            "晶编"    → rod_id
            "质量"    → result (0=OK, 非0=NG)
            "个数"    → defect_count
            "最大面积" → max_area
            "总面积"   → total_area
            "最大长度" → max_length
        """
        # 提取字段（兼容中文和英文 key）
        inspect_id = int(metadata.get("ID", metadata.get("id", 0)))
        rod_id = str(metadata.get("晶编", metadata.get("rod_id", "UNKNOWN")))
        quality = int(metadata.get("质量", metadata.get("OKORNG", 0)))
        defect_count = int(metadata.get("个数", metadata.get("defect_count", 0)))
        max_area = float(metadata.get("最大面积", metadata.get("max_area", 0)))
        total_area = float(metadata.get("总面积", metadata.get("total_area", 0)))
        max_length = float(metadata.get("最大长度", metadata.get("max_length", 0)))

        # 新增字段
        defect_type_raw = str(metadata.get("类型", metadata.get("Type", "")))
        ct = float(metadata.get("检测时长", metadata.get("CT", 0)))
        check_time = str(metadata.get("检测时间", metadata.get("CheckTime", "")))

        # 质量判定：0=OK，非0=NG
        result = "OK" if quality == 0 else "NG"

        # 缺陷类型：优先使用 Halcon 传过来的“类型”字段
        if result == "OK":
            defect_type = ""
        elif defect_type_raw and defect_type_raw != "OK":
            defect_type = defect_type_raw
        else:
            # 回退：根据缺陷信息拼接描述
            parts = []
            if defect_count > 0:
                parts.append(f"{defect_count}处缺陷")
            if max_area > 0:
                parts.append(f"最大面积{max_area:.1f}")
            if max_length > 0:
                parts.append(f"最大长度{max_length:.1f}")
            defect_type = ", ".join(parts) if parts else "NG"

        now = datetime.datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
        upload_time = now.strftime("%Y-%m-%d %H:%M:%S")

        return InspectData(
            rod_id=rod_id,
            result=result,
            image=image,
            defect_type=defect_type,
            defect_count=defect_count,
            max_area=max_area,
            total_area=total_area,
            max_length=max_length,
            inspect_id=inspect_id,
            quality=quality,
            ct=ct,
            check_time=check_time,
            upload_time=upload_time,
            timestamp=timestamp,
            raw_json=metadata,
        )

    # ─────────── 回调分发 ───────────
    def _dispatch(self, data: InspectData):
        """分发数据到所有已注册的回调"""
        for cb in self._callbacks:
            try:
                cb(data)
            except Exception as e:
                logger.error(
                    f"回调执行异常: {e}\n{traceback.format_exc()}"
                )