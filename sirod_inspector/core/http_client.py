"""
MES HTTP 上传客户端
===================
当检测结果为 NG 时，通过 HTTP POST 上传数据到 MES 接口。

请求格式：
{
    "HEAD": {
        "DEST_SYSTEM": "YC01MES",
        "INTF_ID": "QPMES201",
        "SRC_SYSTEM": "YinLieJianCe",
        ...
    },
    "BODY": [
        {
            "BlockCode": "LJ1N55D029141X-B",          ← TCP "晶编"
            "CryptoschisisLength": 5.674,              ← TCP "隐裂长度"
            "FilePath": "http://.../xxx.jpg",          ← TCP "图片路径"
            "Generatedate": "2025-06-05 18:09:23"      ← 上传时间
        }
    ]
}

从 AppConfig 读取：
    http.enabled       bool,  是否启用 MES 上传
    http.url           str,   接口地址
    http.timeout       float, 请求超时（秒）
    http.head          dict,  HEAD 字段（key-value 可增删）——不可为空
    http.body_extra    dict,  (可选) BODY 中需要额外注入的固定字段，如 LineId、MachineId
    http.body_field_map dict, (可选) TCP raw_json 字段 → MES BODY 字段的映射表
                              例: {"缺陷类型": "DefectType", "缺陷数量": "DefectCount"}
                              映射表中的 raw_json 字段会自动带入 BODY
    http.biz_ok_field  str,   (可选) 自定义业务成功判断字段名，如 "code"
    http.biz_ok_value  str,   (可选) 自定义业务成功判断值，如 "0"
                              若不配置则自动检测 code/success/result/status/flag

    image_store.base_dir str, 本地图片存储根目录
    image_store.base_url str, (可选) 图片 HTTP 服务的根 URL，用于将本地路径转为 HTTP 地址
                              例: "http://192.168.1.100:8080/images"
                              若未配置，FilePath 将使用本地磁盘路径（MES 可能无法访问）
"""
import datetime
import json
import os
import threading

from core.logger import get_logger

logger = get_logger("SiRod.Http")

# requests 是可选依赖，缺失时模块仍可导入但上传会失败
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    logger.warning("requests 库未安装，MES HTTP 上传将不可用。请 pip install requests")


class MesHttpClient:
    """MES HTTP 上传客户端（线程安全）"""

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()

    @property
    def is_enabled(self) -> bool:
        return bool(self.config.get("http.enabled", True)) and HAS_REQUESTS

    def build_payload(self, inspect_data) -> dict:
        """根据 InspectData 构建请求体。

        BODY 字段映射：
          BlockCode           ← raw_json["晶编"] 或 data.rod_id
          CryptoschisisLength ← raw_json["隐裂长度"] 或 data.max_length
          FilePath            ← raw_json["图片路径"]（可为空）
          Generatedate        ← 当前时间
        """
        raw = getattr(inspect_data, "raw_json", {}) or {}

        # BlockCode
        block_code = str(raw.get("晶编") or inspect_data.rod_id or "")

        # CryptoschisisLength
        length_val = raw.get("隐裂长度")
        if length_val is None:
            length_val = getattr(inspect_data, "max_length", 0.0)
        try:
            crypto_length = float(length_val)
        except (TypeError, ValueError):
            crypto_length = 0.0

        # FilePath — 优先使用 HTTP URL，本地路径 MES 无法访问
        file_path = str(raw.get("图片路径") or "")
        if file_path and not file_path.startswith(("http://", "https://")):
            # 尝试用配置的 base_url 转换为 HTTP URL
            base_url = self.config.get("image_store.base_url", "")
            base_dir = self.config.get("image_store.base_dir", "")
            if base_url and base_dir and file_path.startswith(base_dir.replace("/", os.sep)):
                try:
                    relative = os.path.relpath(file_path, base_dir)
                    file_path = f"{base_url.rstrip('/')}/{relative.replace(os.sep, '/')}"
                    logger.debug(f"FilePath 已转换为 HTTP URL: {file_path}")
                except ValueError:
                    pass  # 跨驱动器等无法计算相对路径的情况，保留原值
            if not file_path.startswith(("http://", "https://")):
                logger.warning(
                    f"[MES警告] FilePath 是本地路径而非 HTTP URL: {file_path}，"
                    f"MES 可能无法访问。请在 config.json 中配置 image_store.base_url"
                )

        # Generatedate — 用当前时间作为上传时间
        generate_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # HEAD 从配置读取；做浅拷贝避免并发污染
        head_cfg = self.config.get("http.head", {}) or {}
        head = dict(head_cfg) if isinstance(head_cfg, dict) else {}

        body_item = {
            "BlockCode": block_code,
            "CryptoschisisLength": crypto_length,
            "FilePath": file_path,
            "Generatedate": generate_date,
        }

        # ── 从配置注入额外的固定字段（如 LineId、MachineId 等） ──
        body_extra = self.config.get("http.body_extra", {})
        if isinstance(body_extra, dict) and body_extra:
            body_item.update(body_extra)
            logger.debug(f"BODY 已注入额外字段: {list(body_extra.keys())}")

        # ── 按映射表从 TCP raw_json 中提取字段到 BODY ──
        field_map = self.config.get("http.body_field_map", {})
        if isinstance(field_map, dict):
            for raw_key, mes_key in field_map.items():
                if raw_key in raw and raw[raw_key] is not None:
                    body_item[mes_key] = raw[raw_key]

        return {
            "HEAD": head,
            "BODY": [body_item],
        }

    def _check_biz_response(self, body: dict) -> tuple:
        """校验 MES 业务返回值。

        MES 接口 HTTP 状态码通常始终为 200，业务成功/失败通过 JSON body 区分。
        本方法按优先级依次检测常见的业务状态字段：
            1. "code"    → 0 / "0" / "000" / "200" 视为成功
            2. "success" → True / "true" 视为成功
            3. "result"  → "OK" / "SUCCESS" 视为成功（不区分大小写）
            4. "status"  → "OK" / "SUCCESS" / 0 / "0" 视为成功
            5. "flag"    → True / "Y" / "1" 视为成功

        可通过 config.json 的 http.biz_ok_field / http.biz_ok_value 自定义判断字段和值，
        优先级高于上述默认规则。

        返回: (biz_ok: bool, biz_msg: str)
        """
        if not isinstance(body, dict):
            return False, f"响应不是 JSON 对象: {str(body)[:200]}"

        # ── 优先使用用户自定义的判断字段 ──
        custom_field = self.config.get("http.biz_ok_field", "")
        if custom_field and custom_field in body:
            custom_expect = self.config.get("http.biz_ok_value", None)
            actual = body[custom_field]
            if custom_expect is not None:
                biz_ok = str(actual).strip().lower() == str(custom_expect).strip().lower()
            else:
                # 未配置期望值时，truthy 判断
                biz_ok = bool(actual)
            biz_msg = self._extract_biz_msg(body)
            return biz_ok, biz_msg

        # ── 默认规则：按优先级逐个检测 ──
        # 1) code
        if "code" in body:
            code_val = body["code"]
            ok = str(code_val).strip() in ("0", "000", "200")
            return ok, self._extract_biz_msg(body)

        # 2) success
        if "success" in body:
            ok = str(body["success"]).strip().lower() in ("true", "1")
            return ok, self._extract_biz_msg(body)

        # 3) result
        if "result" in body:
            ok = str(body["result"]).strip().upper() in ("OK", "SUCCESS", "TRUE", "1")
            return ok, self._extract_biz_msg(body)

        # 4) status
        if "status" in body:
            sv = str(body["status"]).strip().upper()
            ok = sv in ("OK", "SUCCESS", "0", "TRUE", "1")
            return ok, self._extract_biz_msg(body)

        # 5) flag
        if "flag" in body:
            fv = str(body["flag"]).strip().upper()
            ok = fv in ("TRUE", "Y", "1", "YES", "OK")
            return ok, self._extract_biz_msg(body)

        # 没有找到任何已知状态字段 → 记录警告，默认当作成功（兼容旧行为）
        logger.warning(
            f"[MES警告] 响应中未找到已知业务状态字段(code/success/result/status/flag)，"
            f"无法判断业务是否成功，按成功处理。响应体: {str(body)[:300]}"
        )
        return True, self._extract_biz_msg(body) or "无业务状态字段，默认成功"

    @staticmethod
    def _extract_biz_msg(body: dict) -> str:
        """从 MES 响应体中提取业务描述信息。"""
        for key in ("msg", "message", "Message", "MSG",
                     "desc", "description", "info",
                     "errmsg", "errMsg", "error", "Error",
                     "reason", "Reason", "detail"):
            val = body.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""

    def upload_ng(self, inspect_data) -> tuple:
        """向 MES 接口上传 NG 数据。

        返回: (success: bool, message: str)
        ─────────────────────────────────
        success 为 True 当且仅当：
          1. HTTP 请求发送成功（状态码 2xx）
          2. 响应体 JSON 中的业务状态字段表示成功
        任一环节失败都会返回 False 和具体失败原因。
        """
        if not self.config.get("http.enabled", True):
            msg = "MES 上传未启用"
            logger.info(f"[MES跳过] {msg}")
            return False, msg

        if not HAS_REQUESTS:
            msg = "requests 库未安装，无法上传"
            logger.error(f"[MES失败] {msg}")
            return False, msg

        url = self.config.get("http.url", "")
        if not url:
            msg = "接口地址未配置"
            logger.error(f"[MES失败] {msg}")
            return False, msg

        try:
            timeout = float(self.config.get("http.timeout", 10))
        except (TypeError, ValueError):
            timeout = 10.0

        payload = self.build_payload(inspect_data)
        rod_id = payload["BODY"][0]["BlockCode"]

        if not rod_id:
            msg = "BlockCode 为空，跳过上传"
            logger.warning(f"[MES跳过] {msg}")
            return False, msg

        # ── HEAD 完整性校验 ──
        head = payload.get("HEAD", {})
        if not head:
            msg = "config.json 中 http.head 为空，MES 无法识别数据来源，请配置 DEST_SYSTEM/INTF_ID/SRC_SYSTEM"
            logger.error(f"[MES失败] {msg}")
            return False, msg

        _REQUIRED_HEAD_KEYS = ("DEST_SYSTEM", "INTF_ID", "SRC_SYSTEM")
        missing_keys = [k for k in _REQUIRED_HEAD_KEYS if not head.get(k)]
        if missing_keys:
            logger.warning(
                f"[MES警告] HEAD 中缺少关键字段: {', '.join(missing_keys)}，"
                f"MES 可能无法正确路由此请求。当前 HEAD={head}"
            )

        # 请求日志
        try:
            payload_str = json.dumps(payload, ensure_ascii=False)
        except Exception:
            payload_str = str(payload)
        payload_bytes = payload_str.encode("utf-8")
        logger.info(
            f"[MES请求] URL={url} rod_id={rod_id} "
            f"timeout={timeout}s body_size={len(payload_bytes)}B "
            f"payload={payload_str}"
        )

        # ── 发送请求 ──
        try:
            with self._lock:
                resp = requests.post(
                    url,
                    json=payload,
                    timeout=timeout,
                    allow_redirects=False,   # 禁止自动跟随重定向，防止请求被悄悄转走
                )

            resp_text = resp.text[:500] if resp.text else ""

            # ── 诊断日志：记录响应头关键信息，帮助判断谁在回复 ──
            resp_server = resp.headers.get("Server", "未知")
            resp_via = resp.headers.get("Via", "")
            resp_powered = resp.headers.get("X-Powered-By", "")
            resp_content_type = resp.headers.get("Content-Type", "")
            resp_location = resp.headers.get("Location", "")
            actual_url = resp.url  # requests 实际请求的 URL
            logger.info(
                f"[MES响应诊断] rod_id={rod_id} "
                f"status={resp.status_code} "
                f"actual_url={actual_url} "
                f"Server={resp_server} "
                f"Via={resp_via} "
                f"X-Powered-By={resp_powered} "
                f"Content-Type={resp_content_type} "
                f"Content-Length={resp.headers.get('Content-Length', '?')} "
                f"Location={resp_location} "
                f"resp_body={resp_text}"
            )

            # ── 检测重定向：3xx 说明请求被转发到了别的地方 ──
            if 300 <= resp.status_code < 400:
                msg = (
                    f"请求被重定向到 {resp_location}，"
                    f"数据可能未送达 MES。请检查 URL 配置是否正确"
                )
                logger.error(f"[MES失败] rod_id={rod_id} {msg}")
                return False, msg

            # ── 检测代理/网关特征 ──
            proxy_indicators = []
            if resp_via:
                proxy_indicators.append(f"Via={resp_via}")
            # 常见网关 Server 标识
            gateway_keywords = ("nginx", "apache", "squid", "varnish",
                                "haproxy", "cloudflare", "iis", "openresty")
            if any(kw in resp_server.lower() for kw in gateway_keywords):
                proxy_indicators.append(f"Server={resp_server}")
            if proxy_indicators:
                logger.warning(
                    f"[MES警告] 响应可能来自代理/网关而非 MES 本身: "
                    f"{', '.join(proxy_indicators)}。"
                    f"如果 MES 确认未收到数据，请检查 URL 是否指向 MES 应用端口"
                )

            # ── 检测请求 URL 和实际 URL 不一致（隐式重定向） ──
            if actual_url and actual_url.rstrip("/") != url.rstrip("/"):
                logger.warning(
                    f"[MES警告] 请求 URL 与实际 URL 不一致！"
                    f"配置={url} 实际={actual_url}，数据可能发送到了错误的地址"
                )

            # ── 第一层：检查 HTTP 状态码 ──
            if not resp.ok:
                msg = f"HTTP 请求失败: {resp.status_code} {resp.reason}"
                logger.error(
                    f"[MES失败] rod_id={rod_id} status={resp.status_code} "
                    f"reason={resp.reason} resp={resp_text}"
                )
                return False, msg

            # ── 第二层：解析响应体，校验业务返回值 ──
            try:
                resp_body = resp.json()
            except (ValueError, TypeError):
                # 响应不是合法 JSON — 很可能不是 MES 应用在回复
                msg = (
                    f"HTTP {resp.status_code} 但响应非 JSON "
                    f"(Server={resp_server})，可能不是 MES 在回复: "
                    f"{resp_text[:200]}"
                )
                logger.error(f"[MES失败] rod_id={rod_id} {msg}")
                return False, msg

            biz_ok, biz_msg = self._check_biz_response(resp_body)

            if biz_ok:
                display_msg = f"成功"
                if biz_msg:
                    display_msg += f" ({biz_msg})"
                logger.info(
                    f"[MES成功] rod_id={rod_id} status={resp.status_code} "
                    f"biz_msg={biz_msg} resp={resp_text}"
                )
                return True, display_msg
            else:
                display_msg = f"MES 业务拒绝"
                if biz_msg:
                    display_msg += f": {biz_msg}"
                else:
                    display_msg += f" (响应: {resp_text[:150]})"
                logger.error(
                    f"[MES失败] rod_id={rod_id} HTTP {resp.status_code} "
                    f"但业务返回失败: biz_msg={biz_msg} resp={resp_text}"
                )
                return False, display_msg

        except requests.exceptions.Timeout:
            msg = f"请求超时({timeout}s)"
            logger.error(f"[MES失败] rod_id={rod_id} {msg}")
            return False, msg
        except requests.exceptions.ConnectionError as e:
            msg = f"连接失败: {e}"
            logger.error(f"[MES失败] rod_id={rod_id} 连接异常: {e}")
            return False, msg
        except Exception as e:
            msg = f"异常: {type(e).__name__}: {e}"
            logger.error(f"[MES失败] rod_id={rod_id} 未知异常: {e}", exc_info=True)
            return False, msg
