"""
飞书多维表格同步模块
====================
将检测结果追加写入飞书多维表格。

配置说明（config.json feishu 节）：
    enabled     : true/false
    app_id      : 飞书应用的 App ID
    app_secret  : 飞书应用的 App Secret
    app_token   : 多维表格的 App Token（浏览器地址栏 /base/[这里]）
    table_id    : 具体表格的 Table ID（地址栏 ?table=[这里]）
    base_url    : 飞书 API 根地址（默认 https://open.feishu.cn/open-apis）
"""

import logging
import threading
import time
import datetime
import traceback
from collections import deque

logger = logging.getLogger(__name__)

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    logger.warning("requests 库未安装，飞书同步不可用，请执行: pip install requests")


class FeishuSync:
    """飞书多维表格同步"""

    def __init__(self, config: dict):
        self._config     = config
        self._enabled    = config.get("enabled", False)
        self._app_id     = config.get("app_id", "")
        self._app_secret = config.get("app_secret", "")
        self._app_token  = config.get("app_token", "")
        self._table_id   = config.get("table_id", "")
        self._base_url   = config.get("base_url", "https://open.feishu.cn/open-apis")

        self._token        = ""
        self._token_expire = 0
        self._retry_queue  = deque(maxlen=1000)
        self._lock         = threading.Lock()
        self._running      = False
        self._sync_thread  = None

        # 启动时记录配置状态，方便排查
        logger.info("飞书同步初始化:")
        logger.info(f"  enabled    = {self._enabled}")
        logger.info(f"  app_id     = {self._app_id!r}")
        logger.info(f"  app_secret = {'***' if self._app_secret else '(空)'}")
        logger.info(f"  app_token  = {self._app_token!r}")
        logger.info(f"  table_id   = {self._table_id!r}")
        logger.info(f"  base_url   = {self._base_url}")

    @property
    def is_enabled(self) -> bool:
        return (
            self._enabled
            and bool(self._app_id)
            and bool(self._app_secret)
            and bool(self._app_token)
            and bool(self._table_id)
        )

    # ── 启动 / 停止 ──────────────────────────────────────────────────────────

    def start(self):
        """启动后台同步线程，并在启动时做一次连通性检测"""
        if not HAS_REQUESTS:
            logger.error("[飞书] requests 库未安装，无法启动。请执行: pip install requests")
            return

        # 逐项检查配置，给出精确提示
        missing = []
        if not self._enabled:
            logger.info("[飞书] enabled=false，同步未启用")
            return
        if not self._app_id:
            missing.append("app_id")
        if not self._app_secret:
            missing.append("app_secret")
        if not self._app_token:
            missing.append("app_token（多维表格地址栏 /base/[这里]）")
        if not self._table_id:
            missing.append("table_id（多维表格地址栏 ?table=[这里]）")
        if missing:
            logger.error(f"[飞书] 以下配置未填写，同步无法启动: {', '.join(missing)}")
            return

        # 启动前先测试一次 Token 获取
        logger.info("[飞书] 正在验证 App ID / App Secret...")
        try:
            token = self._get_token()
            logger.info(f"[飞书] Token 获取成功，长度={len(token)}")
        except Exception as e:
            logger.error(f"[飞书] Token 获取失败，同步无法启动: {e}")
            logger.debug(traceback.format_exc())
            return

        # 启动前自动检查并修复表格结构
        logger.info("[飞书] 正在检查多维表格结构...")
        try:
            self._ensure_table_fields()
        except Exception as e:
            logger.warning(f"[飞书] 表格结构检查失败（不影响启动）: {e}")
            logger.debug(traceback.format_exc())

        self._running = True
        self._sync_thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="FeishuSyncThread"
        )
        self._sync_thread.start()
        logger.info("[飞书] 同步线程已启动")

    def stop(self):
        self._running = False
        if self._sync_thread:
            self._sync_thread.join(timeout=5)
        logger.info("[飞书] 同步线程已停止")

    # ── 公开接口 ─────────────────────────────────────────────────────────────

    def push_result(self, rod_id: str, result: str, defect_type: str = "",
                    defect_count: int = 0, duration_ms: int = 0,
                    line_id: str = "PV-B02"):
        """推送单条检测结果到飞书（线程安全）"""
        if not self.is_enabled:
            logger.debug(f"[飞书] push_result 跳过（未启用）: {rod_id}")
            return

        record = {
            "晶棒编号":     rod_id,
            "检测时间":     datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "检测结果":     result,
            "缺陷类型":     defect_type or "-",
            "缺陷数量":     defect_count,
            "检测耗时(ms)": duration_ms,
            "产线":         line_id,
        }
        logger.debug(f"[飞书] 准备推送: {record}")

        try:
            self._append_record(record)
            logger.info(f"[飞书] 推送成功: {rod_id} -> {result}")
        except Exception as e:
            logger.warning(f"[飞书] 推送失败，加入重试队列 (队列长度={len(self._retry_queue)+1}): {e}")
            logger.debug(traceback.format_exc())
            self._retry_queue.append(record)

    # ── 内部方法 ─────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        """获取 tenant_access_token，有效期内复用，到期前 5 分钟自动刷新"""
        with self._lock:
            remaining = self._token_expire - time.time()
            if self._token and remaining > 0:
                logger.debug(f"[飞书] 复用缓存 Token，剩余有效期 {int(remaining)}s")
                return self._token

            logger.info("[飞书] 正在获取新 Token...")
            url = f"{self._base_url}/auth/v3/tenant_access_token/internal"
            logger.debug(f"[飞书] POST {url}")
            logger.debug(f"[飞书] app_id={self._app_id}")

            try:
                resp = requests.post(url, json={
                    "app_id":     self._app_id,
                    "app_secret": self._app_secret,
                }, timeout=5)
            except requests.exceptions.ConnectionError as e:
                raise RuntimeError(f"网络连接失败（无法访问飞书 API，检查网络）: {e}")
            except requests.exceptions.Timeout:
                raise RuntimeError("请求超时（5s），检查网络或飞书 API 是否可访问")

            logger.debug(f"[飞书] Token 响应 HTTP {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                # 常见错误码说明
                code = data.get("code")
                msg  = data.get("msg", "")
                hint = {
                    10003: "app_id 不存在或已被禁用",
                    10014: "app_secret 不正确",
                    10015: "应用未发布（需要在飞书开放平台发布应用）",
                    99991671: "app_id 格式错误",
                }.get(code, "")
                raise RuntimeError(
                    f"获取 Token 失败 code={code} msg={msg}"
                    + (f" → {hint}" if hint else "")
                )

            self._token        = data["tenant_access_token"]
            self._token_expire = time.time() + data.get("expire", 7200) - 300
            logger.info(f"[飞书] Token 获取成功，有效期 {data.get('expire', 7200)}s")
            return self._token

    def _append_record(self, record: dict):
        """追加一条记录到多维表格，并移动到第一行确保最新数据在最上面"""
        if not HAS_REQUESTS:
            raise RuntimeError("requests 库未安装")

        token = self._get_token()
        base_url = (
            f"{self._base_url}/bitable/v1/apps"
            f"/{self._app_token}/tables/{self._table_id}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }
        fields = {
            k: v if isinstance(v, (int, float)) else str(v)
            for k, v in record.items()
        }

        logger.debug(f"[飞书] POST {base_url}/records  fields={fields}")

        # ── Step 1: 插入记录 ─────────────────────────────────────────────────
        try:
            resp = requests.post(
                f"{base_url}/records",
                headers=headers,
                json={"fields": fields},
                timeout=5,
            )
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"网络连接失败: {e}")
        except requests.exceptions.Timeout:
            raise RuntimeError("写入请求超时（5s）")

        logger.debug(f"[飞书] 写入响应 HTTP {resp.status_code}: {resp.text[:300]}")

        if resp.status_code != 200:
            hint = {
                400: "请求参数错误（字段名与表格列名不匹配？）",
                401: "Token 无效或已过期",
                403: "无权限（应用未被添加到该多维表格）",
                404: "app_token 或 table_id 不存在",
                429: "请求过于频繁，触发限流",
            }.get(resp.status_code, "")
            raise RuntimeError(
                f"HTTP {resp.status_code}"
                + (f" → {hint}" if hint else "")
                + f": {resp.text[:200]}"
            )

        body = resp.json()
        if body.get("code") != 0:
            code = body.get("code")
            msg  = body.get("msg", "")
            hint = {
                1254043: "table_id 不存在",
                1254044: "app_token 不存在",
                1254045: "无写入权限，请在多维表格中将应用添加为协作者",
                1254060: "字段类型不匹配（数字列传了文本，或文本列传了数字）",
                1254200: "字段不存在（fields 中的字段名与表格列名不一致）",
            }.get(code, "")
            raise RuntimeError(
                f"业务错误 code={code} msg={msg}"
                + (f" → {hint}" if hint else "")
            )

        # ── Step 2: 获取新插入记录的 record_id ──────────────────────────────
        record_id = body.get("data", {}).get("record", {}).get("record_id", "")
        if not record_id:
            logger.warning("[飞书] 未获取到 record_id，跳过移动到首行")
            return

        logger.debug(f"[飞书] 插入成功 record_id={record_id}，正在移动到首行...")

        # ── Step 3: 移动到第一行 ─────────────────────────────────────────────
        # 飞书 API：PATCH /records/{record_id}/move_location
        # before_record_id 为空字符串 = 移动到最前面
        try:
            move_resp = requests.post(
                f"{base_url}/records/{record_id}/move_location",
                headers=headers,
                json={"before_record_id": ""},   # 空字符串 = 插入到最前面
                timeout=5,
            )
            move_body = move_resp.json()
            if move_resp.status_code == 200 and move_body.get("code") == 0:
                logger.debug("[飞书] 已移动到首行")
            else:
                # 移动失败不影响主流程，只记录警告
                logger.warning(
                    f"[飞书] 移动到首行失败 HTTP {move_resp.status_code} "
                    f"code={move_body.get('code')}: {move_body.get('msg', '')}"
                )
        except Exception as e:
            logger.warning(f"[飞书] 移动到首行异常（不影响写入）: {e}")

    def _ensure_table_fields(self):
        """
        检查多维表格字段是否齐全，缺少的字段自动创建。

        代码里用到的字段：
            晶棒编号、检测时间、检测结果、缺陷类型、缺陷数量、检测耗时(ms)、产线
        """
        # 字段定义：name → field_type
        # 飞书字段类型：1=文本, 2=数字
        REQUIRED_FIELDS = {
            "晶棒编号":     1,
            "检测时间":     1,
            "检测结果":     1,
            "缺陷类型":     1,
            "缺陷数量":     2,
            "检测耗时(ms)": 2,
            "产线":         1,
        }

        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

        # ── 1. 获取当前已有字段 ───────────────────────────────────────────────
        url = (
            f"{self._base_url}/bitable/v1/apps"
            f"/{self._app_token}/tables/{self._table_id}/fields"
        )
        resp = requests.get(url, headers=headers, timeout=5)
        logger.debug(f"[飞书] 获取字段列表 HTTP {resp.status_code}: {resp.text[:300]}")

        if resp.status_code == 403:
            logger.error(
                "[飞书] 获取字段列表 403 无权限 → 请在多维表格右上角「···」"
                "→「添加文档应用」→ 搜索并添加你的应用"
            )
            return
        resp.raise_for_status()

        body = resp.json()
        if body.get("code") != 0:
            logger.warning(f"[飞书] 获取字段列表失败 code={body.get('code')}: {body.get('msg')}")
            return

        existing = {
            item["field_name"]: item.get("type", item.get("field_type"))
            for item in body.get("data", {}).get("items", [])
        }
        logger.info(f"[飞书] 表格现有字段: {list(existing.keys())}")

        # ── 2. 找出缺少的字段并创建 ───────────────────────────────────────────
        create_url = (
            f"{self._base_url}/bitable/v1/apps"
            f"/{self._app_token}/tables/{self._table_id}/fields"
        )
        for field_name, field_type in REQUIRED_FIELDS.items():
            if field_name in existing:
                logger.debug(f"[飞书] 字段已存在: {field_name}")
                continue

            logger.info(f"[飞书] 字段缺失，正在创建: {field_name} (type={field_type})")
            resp2 = requests.post(
                create_url,
                headers=headers,
                json={"field_name": field_name, "type": field_type},
                timeout=5,
            )
            logger.debug(f"[飞书] 创建字段响应 HTTP {resp2.status_code}: {resp2.text[:200]}")

            if resp2.status_code == 403:
                logger.error(
                    f"[飞书] 创建字段 {field_name} 失败 403 → "
                    "应用需要「多维表格」的编辑权限，请在飞书开放平台开通 bitable:app 权限并重新发布"
                )
                continue

            body2 = resp2.json()
            if body2.get("code") == 0:
                logger.info(f"[飞书] 字段创建成功: {field_name}")
            else:
                logger.warning(
                    f"[飞书] 字段 {field_name} 创建失败 "
                    f"code={body2.get('code')}: {body2.get('msg')}"
                )

        logger.info("[飞书] 表格结构检查完成")

    # ── 后台重试 ─────────────────────────────────────────────────────────────

    def _sync_loop(self):
        """后台重试循环，每 30 秒尝试重发失败的记录"""
        logger.info("[飞书] 重试循环已启动")
        while self._running:
            if self._retry_queue:
                record = self._retry_queue.popleft()
                rod_id = record.get("晶棒编号", "?")
                logger.info(f"[飞书] 重试推送: {rod_id}，剩余队列={len(self._retry_queue)}")
                try:
                    self._append_record(record)
                    logger.info(f"[飞书] 重试成功: {rod_id}")
                except Exception as e:
                    logger.warning(f"[飞书] 重试失败，重新入队: {e}")
                    logger.debug(traceback.format_exc())
                    self._retry_queue.appendleft(record)
            time.sleep(30)
        logger.info("[飞书] 重试循环已退出")

    # ── 配置辅助 ─────────────────────────────────────────────────────────────

    def list_bitables(self) -> list[dict]:
        """列出应用有权限访问的所有多维表格（帮助查找 app_token）"""
        if not HAS_REQUESTS:
            raise RuntimeError("requests 库未安装")
        token    = self._get_token()
        list_url = f"{self._base_url}/drive/v1/files"
        headers  = {"Authorization": f"Bearer {token}"}
        resp = requests.get(list_url, headers=headers,
                            params={"folder_token": "", "order_by": "EditedTime",
                                    "direction": "DESC", "page_size": 50},
                            timeout=5)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"列出文件失败: {body.get('msg', body)}")
        return [
            {"name": f.get("name", ""), "app_token": f.get("token", ""), "url": f.get("url", "")}
            for f in body.get("data", {}).get("files", [])
            if f.get("type") == "bitable"
        ]

    def list_tables(self, app_token: str = "") -> list[dict]:
        """列出多维表格下的所有 Sheet（帮助查找 table_id）"""
        if not HAS_REQUESTS:
            raise RuntimeError("requests 库未安装")
        token = self._get_token()
        at    = app_token or self._app_token
        if not at:
            raise RuntimeError("app_token 未提供")
        url  = f"{self._base_url}/bitable/v1/apps/{at}/tables"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=5)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"列出表格失败: {body.get('msg', body)}")
        return [
            {"name": t.get("name", ""), "table_id": t.get("table_id", "")}
            for t in body.get("data", {}).get("items", [])
        ]

    def print_config_hint(self):
        """一键打印所有多维表格和 table_id，帮助填写 config.json"""
        print("=" * 60)
        print("飞书多维表格配置查询")
        print("=" * 60)
        try:
            bitables = self.list_bitables()
            if not bitables:
                print("未找到任何多维表格，请确认应用已被添加到对应表格")
                return
            for bt in bitables:
                print(f"\n表格名称 : {bt['name']}")
                print(f"app_token: {bt['app_token']}")
                print(f"链接     : {bt['url']}")
                try:
                    for t in self.list_tables(bt["app_token"]):
                        print(f"  └─ {t['name']:<20} table_id={t['table_id']}")
                except Exception as e:
                    print(f"  └─ 获取 Sheet 列表失败: {e}")
        except Exception as e:
            print(f"查询失败: {e}")
        print("\n将以上 app_token 和 table_id 填入 config.json 即可。")