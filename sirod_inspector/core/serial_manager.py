"""
串口通信管理器
=============
负责与外部设备（报警灯 / PLC）通过串口通信：
- 打开 / 关闭串口（线程安全、容错）
- 发送 NG 信号 / 复位信号
- 配置热更新（设置页修改后调用 reload() 即可重连）

通信参数（对齐 Halcon 参考实现）：
  波特率 baudrate，8 数据位，无校验，1 停止位，无流控（8N1 none）
  等价 Halcon:
    set_serial_param(h, 9600, 8, 'none', 'none', 1, 1000, 'unchanged')

配置字段（从 AppConfig 读取）：
  serial.enabled       bool,  是否启用串口（默认 True）
  serial.port          str,   端口号（默认 COM3）
  serial.baudrate      int,   波特率（默认 9600）
  serial.timeout       float, 读写超时秒（默认 1）
  serial.ng_signal     str,   NG 信号（默认 "A0 00 01 CC" —— HEX 格式）
  serial.reset_signal  str,   复位信号（默认 "A0 00 00 CC" —— HEX 格式）

信号格式（自动识别）：
  HEX 格式（推荐）：含 0x 前缀 或 含空格/逗号分隔符
      "A0 00 01 CC"            → b'\\xA0\\x00\\x01\\xCC'
      "0xA0,0x00,0x01,0xCC"    → b'\\xA0\\x00\\x01\\xCC'
      "A0,00,01,CC"            → b'\\xA0\\x00\\x01\\xCC'
  ASCII 格式（向后兼容）：无 0x 前缀 无分隔符 或 含非 hex 字符
      "NG\\r\\n"    → b'NG\\r\\n'
      "RESET\\r\\n" → b'RESET\\r\\n'
"""
import re
import threading
import serial

from core.logger import get_logger

logger = get_logger("SiRod.Serial")


class SerialManager:
    def __init__(self, config):
        self.config = config
        self._ser = None
        self._lock = threading.Lock()

    # ─────────── 状态 ───────────
    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # ─────────── 打开 / 关闭 ───────────
    def open(self) -> bool:
        """打开串口。失败返回 False，不抛异常。

        参数对齐 Halcon：8 数据位、无校验、1 停止位、无流控。
        """
        if not self.config.get("serial.enabled", True):
            logger.info("串口通信未启用，跳过打开")
            return False

        # 若已打开先关闭，避免 Windows 下 PermissionError
        self.close()

        port = self.config.get("serial.port", "COM3")
        try:
            baudrate = int(self.config.get("serial.baudrate", 9600))
        except (TypeError, ValueError):
            baudrate = 9600
        try:
            timeout = float(self.config.get("serial.timeout", 1))
        except (TypeError, ValueError):
            timeout = 1.0

        try:
            self._ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,       # 8 数据位
                parity=serial.PARITY_NONE,       # 无校验
                stopbits=serial.STOPBITS_ONE,    # 1 停止位
                timeout=timeout,                 # 读超时
                write_timeout=max(timeout, 1.0), # 写超时（至少 1s）
                xonxoff=False,                   # 无软件流控
                rtscts=False,                    # 无 RTS/CTS 硬件流控
                dsrdtr=False,                    # 无 DSR/DTR 硬件流控
            )
            logger.info(
                f"串口已打开: {port} @ {baudrate} 8N1 none "
                f"(timeout={timeout}s)"
            )
            return True
        except Exception as e:
            logger.error(f"串口打开失败 ({port}): {e}")
            self._ser = None
            return False

    def close(self):
        """关闭串口（幂等）"""
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                    logger.info("串口已关闭")
                except Exception as e:
                    logger.error(f"串口关闭失败: {e}")
                finally:
                    self._ser = None

    def reload(self) -> bool:
        """配置修改后重新打开（供设置页调用）"""
        self.close()
        return self.open()

    # ─────────── 发送 ───────────
    def _send_raw(self, payload: bytes, tag: str = "") -> bool:
        if not self.is_open:
            logger.warning(
                f"串口未打开，{tag}发送被忽略: {self._format_hex(payload)}"
            )
            return False
        try:
            with self._lock:
                self._ser.write(payload)
                self._ser.flush()
            logger.info(
                f"串口已发送 {tag}({len(payload)} 字节): "
                f"{self._format_hex(payload)}"
            )
            return True
        except Exception as e:
            logger.error(f"串口发送失败: {e}", exc_info=True)
            return False

    @staticmethod
    def _format_hex(payload: bytes) -> str:
        """把字节序列格式化为可读的 hex 字符串用于日志"""
        if not payload:
            return "<empty>"
        return " ".join(f"{b:02X}" for b in payload)

    @classmethod
    def _encode(cls, signal: str) -> bytes:
        """把配置字符串转成真实字节。

        优先按 HEX 格式解析：
          "A0 00 01 CC" / "0xA0,0x00,0x01,0xCC" → b"\\xA0\\x00\\x01\\xCC"
        HEX 解析失败才按 ASCII 处理（支持 \\r \\n 转义）。
        """
        if not signal:
            return b""

        # 只 strip 空格/tab，避免吃掉用户故意写在末尾的 \r\n 真实字符
        s = signal.strip(' \t')

        # 尝试 HEX（HEX 路径允许 strip 所有空白）
        hex_bytes = cls._try_parse_hex(s.strip())
        if hex_bytes is not None:
            return hex_bytes

        # 回退：按 ASCII 处理，支持 \r \n \t 等转义
        try:
            return s.encode("utf-8").decode("unicode_escape").encode("utf-8")
        except Exception:
            return s.encode("utf-8", errors="replace")

    @staticmethod
    def _try_parse_hex(s: str):
        """尝试把字符串识别为 HEX 字节序列。失败返回 None。

        识别规则（要足够明确，避免把 ASCII 误判为 HEX）：
          - 含 "0x" / "0X" 前缀               → 肯定是 HEX
          - 含空格 / 逗号 / 分号 / 方括号分隔  → HEX（前提是所有 token 都是 hex）
          - 其他情况（裸字符串如 "NG"）        → 不当作 HEX
        """
        if not s:
            return None

        has_0x_prefix = bool(re.search(r'0[xX]', s))
        has_separator = bool(re.search(r'[\s,;\[\]]', s))

        if not (has_0x_prefix or has_separator):
            return None  # 无明确格式标志，按 ASCII 处理

        # 去掉 0x 前缀和所有分隔符
        cleaned = re.sub(r'0[xX]', '', s)
        cleaned = re.sub(r'[\s,;\-_\[\]]+', '', cleaned)

        if not cleaned:
            return None
        if len(cleaned) % 2 != 0:
            return None
        if not re.fullmatch(r'[0-9a-fA-F]+', cleaned):
            return None

        try:
            return bytes.fromhex(cleaned)
        except ValueError:
            return None

    # ─────────── 业务接口 ───────────
    def send_ng(self) -> bool:
        """发送 NG 信号（内容由 serial.ng_signal 配置，默认 A0 00 01 CC）"""
        signal = self.config.get("serial.ng_signal", "A0 00 01 CC")
        return self._send_raw(self._encode(signal), tag="NG ")

    def send_reset(self) -> bool:
        """发送复位信号（内容由 serial.reset_signal 配置，默认 A0 00 00 CC）"""
        signal = self.config.get("serial.reset_signal", "A0 00 00 CC")
        return self._send_raw(self._encode(signal), tag="RESET ")
