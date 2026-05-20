"""
配置管理模块
============
单例模式管理 config.json，支持 get/set/save。
"""

import json
import os
import copy
import logging

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "tcp": {"host": "127.0.0.1", "port": 3000},
    "database": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "b_xmartsql",
        "table": "squarstickresult",
    },
    "feishu": {
        "enabled": False,
        "app_id": "",
        "app_secret": "",
        "table_id": "",
        "base_url": "https://open.feishu.cn/open-apis",
    },
    "image_store": {"enabled": True, "base_dir": "D:/SiRod/images"},
    "shift": {"reset_times": ["08:00", "20:00"]},
    "line_id": "PV-B02",
    "serial": {
        "enabled": True,
        "port": "COM3",
        "baudrate": 9600,
        "timeout": 1,
        "ng_signal": "A0 00 01 CC",
        "reset_signal": "A0 00 00 CC",
    },
    "alarm": {
        "enabled": True,
    },
    "http": {
        "enabled": True,
        "url": "http://10.31.20.29/MesAPI/Api/WMSToMESByProcedure",
        "timeout": 10,
        "head": {
            "DEST_SYSTEM": "YC01MES",
            "INTF_ID": "QPMES201",
            "SRC_SYSTEM": "YinLieJianCe",
            "SRC_MSGID": "",
            "BACKUP1": "QPMES201_CryptoschisisDataEM",
            "BACKUP2": "GRZ",
        },
    },
}


class AppConfig:
    """应用配置单例"""

    _instance = None

    def __new__(cls, config_path: str = ""):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config_path: str = ""):
        if self._initialized:
            return
        self._initialized = True

        if not config_path:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base, "config.json")

        self._path = config_path
        self._data = copy.deepcopy(_DEFAULT_CONFIG)
        self.load()

    def load(self):
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._deep_update(self._data, loaded)
                logger.info(f"配置已加载: {self._path}")
            except Exception as e:
                logger.warning(f"加载配置失败，使用默认值: {e}")
        else:
            logger.info("配置文件不存在，使用默认值")
            self.save()

    def save(self):
        # 原子写：先写 .tmp 再 os.replace，防止程序在写一半时挂导致
        # config.json 残缺（json.dump 是流式写，崩了会得到坏 JSON，
        # 下次启动加载失败）。os.replace 在 Windows / POSIX 都是原子的。
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=4, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass    # 某些 FS 不支持 fsync — 软失败可接受
            os.replace(tmp_path, self._path)
            logger.info(f"配置已保存: {self._path}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            # 失败时尽量清理 .tmp
            try:
                if os.path.exists(self._path + ".tmp"):
                    os.remove(self._path + ".tmp")
            except OSError:
                pass

    def get(self, key: str, default=None):
        keys = key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default
        return val

    def set(self, key: str, value):
        keys = key.split(".")
        d = self._data
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    @property
    def data(self) -> dict:
        return self._data

    @property
    def path(self) -> str:
        return self._path

    def _deep_update(self, base: dict, override: dict):
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_update(base[k], v)
            else:
                base[k] = v

    @classmethod
    def reset_instance(cls):
        cls._instance = None
