"""
MySQL 数据库读写模块
====================
连接 MySQL，提供检测记录的写入与查询接口。

数据库字段映射（Halcon JSON → 数据库列）：
    ID       → ID (BIGINT)
    晶编     → SquareNumber (VARCHAR)
    质量     → Quality (INT, 0=OK, 1=NG)
    个数     → DefectNumber (INT)
    最大面积 → MaxArea (DOUBLE)
    总面积   → TotalArea (DOUBLE)
    最大长度 → MaxLength (DOUBLE)
    类型     → Type (VARCHAR, OK/隐裂/崩边)
    检测时长 → CT (DOUBLE, 毫秒)
    检测时间 → CheckTime (VARCHAR, Halcon 原始格式)
    (自动)   → UploadTime (DATETIME, 数据上传时间)
    (自动)   → ImagePath (TEXT, 图像存储路径)
    (自动)   → LineID (VARCHAR, 产线标识)
"""

import logging
import datetime
import threading

logger = logging.getLogger(__name__)

try:
    import pymysql
    pymysql.install_as_MySQLdb()
    HAS_PYMYSQL = True
except ImportError:
    HAS_PYMYSQL = False
    logger.warning("pymysql 未安装，数据库功能不可用。请执行: pip install pymysql")


# ─────────────────────────────────────────────
#  期望的表结构定义
# ─────────────────────────────────────────────
# 每个元素: (列名, 完整的列定义 SQL, 列注释说明)
# 用于 CREATE TABLE 和 ALTER TABLE ADD COLUMN
_EXPECTED_COLUMNS = [
    ("id",            "BIGINT AUTO_INCREMENT PRIMARY KEY",                    "自增主键"),
    ("ID",            "BIGINT DEFAULT 0",                                     "检测ID"),
    ("SquareNumber",  "VARCHAR(50) DEFAULT ''",                               "晶棒编号"),
    ("Quality",       "INT DEFAULT 0",                                        "质量(0=OK,1=NG)"),
    ("DefectNumber",  "INT DEFAULT 0",                                        "缺陷数量"),
    ("MaxArea",       "DOUBLE DEFAULT 0",                                     "最大缺陷面积"),
    ("TotalArea",     "DOUBLE DEFAULT 0",                                     "缺陷总面积"),
    ("MaxLength",     "DOUBLE DEFAULT 0",                                     "最大缺陷长度"),
    ("Type",          "VARCHAR(50) DEFAULT ''",                               "缺陷类型"),
    ("CT",            "DOUBLE DEFAULT 0",                                     "检测时长(ms)"),
    ("CheckTime",     "VARCHAR(100) DEFAULT ''",                              "检测时间(Halcon原始格式)"),
    ("UploadTime",    "DATETIME DEFAULT CURRENT_TIMESTAMP",                   "数据上传时间"),
    ("ImagePath",     "TEXT DEFAULT NULL",                                    "图像存储路径"),
    ("LineID",        "VARCHAR(20) DEFAULT 'PV-B02'",                         "产线标识"),
    # 兼容旧字段（如果旧表中有这些字段，保留不删除）
    ("rod_id",        "VARCHAR(50) DEFAULT ''",                               "晶棒编号(旧)"),
    ("inspect_time",  "DATETIME DEFAULT CURRENT_TIMESTAMP",                   "检测时间(旧)"),
    ("result",        "VARCHAR(10) DEFAULT ''",                               "结果(旧)"),
    ("defect_type",   "VARCHAR(50) DEFAULT ''",                               "缺陷类型(旧)"),
    ("defect_count",  "INT DEFAULT 0",                                        "缺陷数量(旧)"),
    ("duration_ms",   "INT DEFAULT 0",                                        "检测耗时ms(旧)"),
    ("image_path",    "TEXT DEFAULT NULL",                                    "图像路径(旧)"),
    ("line_id",       "VARCHAR(20) DEFAULT 'PV-B02'",                         "产线标识(旧)"),
    ("created_at",    "DATETIME DEFAULT CURRENT_TIMESTAMP",                   "创建时间(旧)"),
]

# 新表必须有的列（不含旧兼容列），用于 CREATE TABLE
_NEW_COLUMNS = [
    ("id",            "BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键'"),
    ("ID",            "BIGINT DEFAULT 0 COMMENT '检测ID'"),
    ("SquareNumber",  "VARCHAR(50) DEFAULT '' COMMENT '晶棒编号'"),
    ("Quality",       "INT DEFAULT 0 COMMENT '质量(0=OK,1=NG)'"),
    ("DefectNumber",  "INT DEFAULT 0 COMMENT '缺陷数量'"),
    ("MaxArea",       "DOUBLE DEFAULT 0 COMMENT '最大缺陷面积'"),
    ("TotalArea",     "DOUBLE DEFAULT 0 COMMENT '缺陷总面积'"),
    ("MaxLength",     "DOUBLE DEFAULT 0 COMMENT '最大缺陷长度'"),
    ("Type",          "VARCHAR(50) DEFAULT '' COMMENT '缺陷类型'"),
    ("CT",            "DOUBLE DEFAULT 0 COMMENT '检测时长(ms)'"),
    ("CheckTime",     "VARCHAR(100) DEFAULT '' COMMENT '检测时间(Halcon原始格式)'"),
    ("UploadTime",    "DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '数据上传时间'"),
    ("ImagePath",     "TEXT DEFAULT NULL COMMENT '图像存储路径'"),
    ("LineID",        "VARCHAR(20) DEFAULT 'PV-B02' COMMENT '产线标识'"),
]

# 需要自动补全的列（当旧表存在但缺少这些列时，自动 ALTER TABLE ADD COLUMN）
_COLUMNS_TO_ADD = {
    "ID":            "BIGINT DEFAULT 0 COMMENT '检测ID'",
    "SquareNumber":  "VARCHAR(50) DEFAULT '' COMMENT '晶棒编号'",
    "Quality":       "INT DEFAULT 0 COMMENT '质量(0=OK,1=NG)'",
    "DefectNumber":  "INT DEFAULT 0 COMMENT '缺陷数量'",
    "MaxArea":       "DOUBLE DEFAULT 0 COMMENT '最大缺陷面积'",
    "TotalArea":     "DOUBLE DEFAULT 0 COMMENT '缺陷总面积'",
    "MaxLength":     "DOUBLE DEFAULT 0 COMMENT '最大缺陷长度'",
    "Type":          "VARCHAR(50) DEFAULT '' COMMENT '缺陷类型'",
    "CT":            "DOUBLE DEFAULT 0 COMMENT '检测时长(ms)'",
    "CheckTime":     "VARCHAR(100) DEFAULT '' COMMENT '检测时间(Halcon原始格式)'",
    "UploadTime":    "DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '数据上传时间'",
    "ImagePath":     "TEXT DEFAULT NULL COMMENT '图像存储路径'",
    "LineID":        "VARCHAR(20) DEFAULT 'PV-B02' COMMENT '产线标识'",
}


class Database:
    """MySQL 数据库操作封装"""

    def __init__(self, config: dict):
        """
        Parameters
        ----------
        config : dict
            数据库配置，包含 host, port, user, password, database, table
        """
        self._config = config
        self._conn = None
        self._lock = threading.Lock()
        self._table = config.get("table", "squarstickresult")
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self):
        """连接数据库并确保表存在"""
        if not HAS_PYMYSQL:
            logger.error("pymysql 未安装，无法连接数据库")
            return False
        try:
            self._conn = pymysql.connect(
                host=self._config.get("host", "127.0.0.1"),
                port=int(self._config.get("port", 3306)),
                user=self._config.get("user", "root"),
                password=self._config.get("password", "123456"),
                database=self._config.get("database", "b_xmartsql"),
                charset="utf8mb4",
                autocommit=True,
                connect_timeout=5,
            )
            self._connected = True
            logger.info(
                f"数据库连接成功: "
                f"{self._config.get('host')}:{self._config.get('port')}"
            )
            self._ensure_table()
            return True
        except Exception as e:
            self._connected = False
            logger.error(f"数据库连接失败: {e}")
            return False

    def disconnect(self):
        """关闭数据库连接"""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._connected = False
            logger.info("数据库连接已关闭")

    # ─────────── 表结构管理 ───────────

    def _ensure_table(self):
        """
        确保检测结果表存在，并自动补全缺失的字段。

        逻辑：
        1. 如果表不存在 → 创建新表（完整新字段）
        2. 如果表已存在 → 检查每个期望字段是否存在，缺失则 ALTER TABLE ADD COLUMN
        """
        try:
            with self._lock:
                cursor = self._conn.cursor()

                # 1. 检查表是否存在
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    (self._config.get("database", "b_xmartsql"), self._table)
                )
                table_exists = cursor.fetchone()[0] > 0

                if not table_exists:
                    # 表不存在，创建新表
                    self._create_new_table(cursor)
                else:
                    # 表已存在，检查并补全缺失字段
                    self._migrate_table(cursor)

                cursor.close()

            logger.info(f"数据表 {self._table} 已就绪")

        except Exception as e:
            logger.error(f"确保数据表失败: {e}", exc_info=True)

    def _create_new_table(self, cursor):
        """创建新的检测结果表"""
        col_defs = ",\n            ".join(
            f"`{name}` {definition}" for name, definition in _NEW_COLUMNS
        )
        sql = f"""
        CREATE TABLE `{self._table}` (
            {col_defs},
            INDEX `idx_SquareNumber` (`SquareNumber`),
            INDEX `idx_CheckTime` (`CheckTime`),
            INDEX `idx_Quality` (`Quality`),
            INDEX `idx_Type` (`Type`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='方棒检测结果表';
        """
        cursor.execute(sql)
        logger.info(f"数据表 {self._table} 已创建（新表结构）")

    def _migrate_table(self, cursor):
        """
        检查现有表中的字段，尝试自动添加缺失的字段。

        使用 SHOW COLUMNS 获取现有字段列表，对比 _COLUMNS_TO_ADD 中的期望字段，
        缺失的字段通过 ALTER TABLE ADD COLUMN 添加。
        如果 ALTER 权限不足，记录警告并继续运行（只使用已有字段写入）。
        """
        # 获取现有字段名（不区分大小写比较）
        cursor.execute(f"SHOW COLUMNS FROM `{self._table}`")
        existing_columns_raw = cursor.fetchall()
        # SHOW COLUMNS 返回 (Field, Type, Null, Key, Default, Extra)
        existing_columns = {row[0].lower() for row in existing_columns_raw}

        logger.info(
            f"表 {self._table} 现有字段: "
            f"{sorted(existing_columns)}"
        )

        # 检查并添加缺失字段
        added_columns = []
        failed_columns = []
        for col_name, col_definition in _COLUMNS_TO_ADD.items():
            if col_name.lower() not in existing_columns:
                try:
                    alter_sql = (
                        f"ALTER TABLE `{self._table}` "
                        f"ADD COLUMN `{col_name}` {col_definition}"
                    )
                    cursor.execute(alter_sql)
                    added_columns.append(col_name)
                    logger.info(
                        f"自动添加字段: {col_name} → {self._table}"
                    )
                except Exception as e:
                    failed_columns.append(col_name)
                    err_code = getattr(e, 'args', [None])[0] if hasattr(e, 'args') else None
                    if err_code == 1142:
                        # 权限不足，只记录一次警告
                        pass
                    else:
                        logger.warning(
                            f"添加字段 {col_name} 失败: {e}"
                        )

        if failed_columns:
            logger.warning(
                f"ALTER 权限不足，无法添加 {len(failed_columns)} 个字段: "
                f"{failed_columns}。程序将只使用表中已有字段写入数据。"
            )

        if added_columns:
            logger.info(
                f"表 {self._table} 已自动补全 "
                f"{len(added_columns)} 个字段: {added_columns}"
            )
        elif not failed_columns:
            logger.debug(f"表 {self._table} 字段完整，无需补全")

    # ─────────── 数据写入 ───────────

    def save_result(self, rod_id: str = "", result: str = "OK",
                    defect_type: str = "", defect_count: int = 0,
                    image_path: str = None, line_id: str = "PV-B02",
                    inspect_id: int = 0, quality: int = 0,
                    max_area: float = 0.0, total_area: float = 0.0,
                    max_length: float = 0.0, ct: float = 0.0,
                    check_time: str = "", upload_time: str = "",
                    duration_ms: int = 0) -> bool:
        """
        保存检测结果到数据库。

        同时写入新字段和旧字段，确保兼容性。

        Parameters
        ----------
        rod_id : str
            晶棒编号
        result : str
            "OK" 或 "NG"
        defect_type : str
            缺陷类型（如 "隐裂"、"崩边"）
        defect_count : int
            缺陷数量
        image_path : str
            图像存储路径
        line_id : str
            产线标识
        inspect_id : int
            检测ID（来自 Halcon）
        quality : int
            质量（0=OK, 1=NG）
        max_area : float
            最大缺陷面积
        total_area : float
            缺陷总面积
        max_length : float
            最大缺陷长度
        ct : float
            检测时长（毫秒）
        check_time : str
            检测时间（Halcon 原始格式）
        upload_time : str
            数据上传时间
        duration_ms : int
            检测耗时（旧字段兼容）

        Returns
        -------
        bool
            是否写入成功
        """
        if not self._connected:
            logger.warning("数据库未连接，跳过写入")
            return False

        now = datetime.datetime.now()
        if not upload_time:
            upload_time = now.strftime("%Y-%m-%d %H:%M:%S")
        if duration_ms == 0 and ct > 0:
            duration_ms = int(ct)

        # 先检查表中实际有哪些字段，动态构建 INSERT 语句
        try:
            columns, values = self._build_insert_data(
                inspect_id=inspect_id,
                rod_id=rod_id,
                quality=quality,
                defect_count=defect_count,
                max_area=max_area,
                total_area=total_area,
                max_length=max_length,
                defect_type=defect_type,
                ct=ct,
                check_time=check_time,
                upload_time=upload_time,
                image_path=image_path,
                line_id=line_id,
                result=result,
                duration_ms=duration_ms,
                now=now,
            )

            col_str = ", ".join(f"`{c}`" for c in columns)
            placeholders = ", ".join(["%s"] * len(columns))
            # 使用 INSERT ... ON DUPLICATE KEY UPDATE 避免主键重复报错
            # 当 ID 已存在时，更新其余所有字段
            update_parts = []
            for c in columns:
                if c.lower() != "id":  # 主键不需要更新
                    update_parts.append(f"`{c}`=VALUES(`{c}`)")
            if update_parts:
                update_clause = ", ".join(update_parts)
                sql = (
                    f"INSERT INTO `{self._table}` ({col_str}) "
                    f"VALUES ({placeholders}) "
                    f"ON DUPLICATE KEY UPDATE {update_clause}"
                )
            else:
                sql = f"INSERT INTO `{self._table}` ({col_str}) VALUES ({placeholders})"

            with self._lock:
                cursor = self._conn.cursor()
                cursor.execute(sql, values)
                cursor.close()

            logger.info(
                f"检测结果已写入数据库: "
                f"SquareNumber={rod_id}, Quality={quality}, Type={defect_type}"
            )
            return True

        except Exception as e:
            logger.error(f"写入数据库失败: {e}", exc_info=True)
            self._try_reconnect()
            return False

    def _build_insert_data(self, **kwargs) -> tuple:
        """
        根据表中实际存在的字段，动态构建 INSERT 的列名和值列表。

        这样即使表结构不完全一致（旧表/新表/混合表），也能正确写入。
        """
        # 获取表中实际存在的字段及其属性
        # SHOW COLUMNS 返回 (Field, Type, Null, Key, Default, Extra)
        auto_increment_cols = set()  # 自增列集合
        try:
            with self._lock:
                cursor = self._conn.cursor()
                cursor.execute(f"SHOW COLUMNS FROM `{self._table}`")
                rows = cursor.fetchall()
                cursor.close()
            existing = {row[0].lower(): row[0] for row in rows}
            for row in rows:
                # row[5] 是 Extra 字段，包含 auto_increment 信息
                if row[5] and "auto_increment" in str(row[5]).lower():
                    auto_increment_cols.add(row[0].lower())
        except Exception:
            # 如果查询失败，使用新字段列表
            existing = {c.lower(): c for c, _ in _NEW_COLUMNS if c != "id"}
            auto_increment_cols = {"id"}

        # 字段名 → 值 的映射（新字段）
        field_map = {
            "ID":            kwargs.get("inspect_id", 0),
            "SquareNumber":  kwargs.get("rod_id", ""),
            "Quality":       kwargs.get("quality", 0),
            "DefectNumber":  kwargs.get("defect_count", 0),
            "MaxArea":       kwargs.get("max_area", 0.0),
            "TotalArea":     kwargs.get("total_area", 0.0),
            "MaxLength":     kwargs.get("max_length", 0.0),
            "Type":          kwargs.get("defect_type", ""),
            "CT":            kwargs.get("ct", 0.0),
            "CheckTime":     kwargs.get("check_time", ""),
            "UploadTime":    kwargs.get("upload_time", ""),
            "ImagePath":     kwargs.get("image_path"),
            "LineID":        kwargs.get("line_id", "PV-B02"),
            # 旧字段兼容
            "rod_id":        kwargs.get("rod_id", ""),
            "inspect_time":  kwargs.get("now"),
            "result":        kwargs.get("result", "OK"),
            "defect_type":   kwargs.get("defect_type", ""),
            "defect_count":  kwargs.get("defect_count", 0),
            "duration_ms":   kwargs.get("duration_ms", 0),
            "image_path":    kwargs.get("image_path"),
            "line_id":       kwargs.get("line_id", "PV-B02"),
        }

        columns = []
        values = []

        for field_name, field_value in field_map.items():
            # 检查字段是否存在于表中（不区分大小写）
            actual_name = existing.get(field_name.lower())
            if not actual_name:
                continue
            # 跳过自增列（AUTO_INCREMENT），不需要手动写入
            if actual_name.lower() in auto_increment_cols:
                continue
            # 使用表中实际的列名（保持大小写一致）
            columns.append(actual_name)
            values.append(field_value)

        return columns, values

    # ─────────── 数据查询 ───────────

    def query_records(self, date_from: str = None, date_to: str = None,
                      rod_id: str = None, result: str = None,
                      page: int = 1, page_size: int = 10) -> tuple:
        """
        查询检测记录。

        自动适配新旧表结构：
        - 新表使用 SquareNumber/Quality/CheckTime/Type 等字段
        - 旧表使用 rod_id/result/inspect_time/defect_type 等字段
        - 返回结果统一规范化为旧字段名，确保 UI 兼容

        Returns
        -------
        tuple
            (records_list, total_count)
        """
        if not self._connected:
            return [], 0

        # 检测表结构，决定使用哪套字段名
        col_map = self._detect_column_mapping()

        # 构建查询条件
        conditions = []
        params = []

        time_col = col_map.get("time_col", "inspect_time")
        rod_col = col_map.get("rod_col", "rod_id")
        result_col = col_map.get("result_col", "result")

        if date_from:
            conditions.append(f"`{time_col}` >= %s")
            params.append(date_from)
        if date_to:
            conditions.append(f"`{time_col}` <= %s")
            params.append(date_to)
        if rod_id:
            conditions.append(f"`{rod_col}` LIKE %s")
            params.append(f"%{rod_id}%")
        if result and result != "全部":
            if result_col == "Quality":
                # 新表用 Quality 字段
                quality_val = 0 if result == "OK" else 1
                conditions.append(f"`{result_col}` = %s")
                params.append(quality_val)
            else:
                conditions.append(f"`{result_col}` = %s")
                params.append(result)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        offset = (page - 1) * page_size

        try:
            with self._lock:
                cursor = self._conn.cursor(pymysql.cursors.DictCursor)

                # 查询总数
                cursor.execute(
                    f"SELECT COUNT(*) as cnt FROM `{self._table}`{where}",
                    params
                )
                total = cursor.fetchone()["cnt"]

                # 查询分页数据
                order_col = time_col
                cursor.execute(
                    f"SELECT * FROM `{self._table}`{where} "
                    f"ORDER BY `{order_col}` DESC LIMIT %s OFFSET %s",
                    params + [page_size, offset]
                )
                records = cursor.fetchall()
                cursor.close()

            # 规范化字段名，确保 UI 兼容
            normalized = [self._normalize_record(r) for r in records]
            return normalized, total

        except Exception as e:
            logger.error(f"查询记录失败: {e}")
            return [], 0

    def _detect_column_mapping(self) -> dict:
        """
        检测表中实际存在的字段，返回查询时使用的列名映射。
        优先使用新字段名，回退到旧字段名。
        """
        try:
            with self._lock:
                cursor = self._conn.cursor()
                cursor.execute(f"SHOW COLUMNS FROM `{self._table}`")
                rows = cursor.fetchall()
                cursor.close()
            existing = {row[0].lower() for row in rows}
        except Exception:
            existing = set()

        mapping = {}

        # 时间字段
        if "uploadtime" in existing:
            mapping["time_col"] = "UploadTime"
        elif "checktime" in existing:
            mapping["time_col"] = "CheckTime"
        else:
            mapping["time_col"] = "inspect_time"

        # 编号字段
        if "squarenumber" in existing:
            mapping["rod_col"] = "SquareNumber"
        else:
            mapping["rod_col"] = "rod_id"

        # 结果字段
        if "quality" in existing:
            mapping["result_col"] = "Quality"
        else:
            mapping["result_col"] = "result"

        return mapping

    def _normalize_record(self, record: dict) -> dict:
        """
        将数据库记录规范化为 UI 期望的统一格式。

        UI (history_page) 期望的 key:
            id, rod_id, inspect_time, result, defect_type,
            defect_count, duration_ms, line_id
        """
        normalized = dict(record)

        # rod_id
        if "rod_id" not in normalized and "SquareNumber" in normalized:
            normalized["rod_id"] = normalized["SquareNumber"]

        # inspect_time
        if "inspect_time" not in normalized:
            if "UploadTime" in normalized:
                normalized["inspect_time"] = normalized["UploadTime"]
            elif "CheckTime" in normalized:
                normalized["inspect_time"] = normalized["CheckTime"]

        # result
        if "result" not in normalized and "Quality" in normalized:
            normalized["result"] = "OK" if normalized["Quality"] == 0 else "NG"

        # defect_type
        if "defect_type" not in normalized and "Type" in normalized:
            normalized["defect_type"] = normalized["Type"]

        # defect_count
        if "defect_count" not in normalized and "DefectNumber" in normalized:
            normalized["defect_count"] = normalized["DefectNumber"]

        # duration_ms
        if "duration_ms" not in normalized and "CT" in normalized:
            normalized["duration_ms"] = int(normalized["CT"])

        # line_id
        if "line_id" not in normalized and "LineID" in normalized:
            normalized["line_id"] = normalized["LineID"]

        return normalized

    # ─────────── 统计查询 ───────────

    def get_stats(self, date_from: str = None, date_to: str = None) -> dict:
        """获取统计数据"""
        if not self._connected:
            return {"total": 0, "ok": 0, "ng": 0, "pass_rate": 0.0}

        col_map = self._detect_column_mapping()
        time_col = col_map.get("time_col", "inspect_time")
        result_col = col_map.get("result_col", "result")

        conditions = []
        params = []
        if date_from:
            conditions.append(f"`{time_col}` >= %s")
            params.append(date_from)
        if date_to:
            conditions.append(f"`{time_col}` <= %s")
            params.append(date_to)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""

        try:
            with self._lock:
                cursor = self._conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute(
                    f"SELECT `{result_col}`, COUNT(*) as cnt "
                    f"FROM `{self._table}`{where} GROUP BY `{result_col}`",
                    params
                )
                rows = cursor.fetchall()
                cursor.close()

            if result_col == "Quality":
                ok = sum(r["cnt"] for r in rows if r[result_col] == 0)
                ng = sum(r["cnt"] for r in rows if r[result_col] != 0)
            else:
                ok = sum(r["cnt"] for r in rows if r[result_col] == "OK")
                ng = sum(r["cnt"] for r in rows if r[result_col] == "NG")

            total = ok + ng
            pass_rate = (ok / total * 100) if total > 0 else 0.0

            return {
                "total": total,
                "ok": ok,
                "ng": ng,
                "pass_rate": round(pass_rate, 2),
            }
        except Exception as e:
            logger.error(f"获取统计数据失败: {e}")
            return {"total": 0, "ok": 0, "ng": 0, "pass_rate": 0.0}

    def get_hourly_stats(self, date: str = None) -> list:
        """获取按小时统计的检测量"""
        if not self._connected:
            return []
        if not date:
            date = datetime.date.today().isoformat()

        col_map = self._detect_column_mapping()
        time_col = col_map.get("time_col", "inspect_time")
        result_col = col_map.get("result_col", "result")

        try:
            with self._lock:
                cursor = self._conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute(
                    f"SELECT HOUR(`{time_col}`) as hour, "
                    f"`{result_col}` as result_val, COUNT(*) as cnt "
                    f"FROM `{self._table}` "
                    f"WHERE DATE(`{time_col}`) = %s "
                    f"GROUP BY HOUR(`{time_col}`), `{result_col}` "
                    f"ORDER BY hour",
                    (date,)
                )
                rows = cursor.fetchall()
                cursor.close()

            # 规范化结果
            normalized = []
            for row in rows:
                r = dict(row)
                if result_col == "Quality":
                    r["result"] = "OK" if r.get("result_val") == 0 else "NG"
                else:
                    r["result"] = r.get("result_val", "")
                r["hour"] = r.get("hour", 0)
                r["cnt"] = r.get("cnt", 0)
                normalized.append(r)

            return normalized
        except Exception as e:
            logger.error(f"获取小时统计失败: {e}")
            return []

    # ─────────── 重连 ───────────

    def _try_reconnect(self):
        """尝试重连"""
        try:
            self.disconnect()
            self.connect()
        except Exception as e:
            logger.error(f"重连失败: {e}")