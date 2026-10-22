#!/usr/bin/env python3
"""
TreeSTRM 核心配置管理模块（全项目唯一配置来源）
"""

import os
import sys
import json
import shutil
import threading
from typing import Callable, Dict, Any, List, Optional
from pydantic import BaseModel

BASE_DIR = "/opt/treestrm"
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CONFIG_BACKUP_FILE = CONFIG_FILE + ".bak"
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_FILE = os.path.join(DATA_DIR, "history.json")
SPEED_FILE = os.path.join(DATA_DIR, "speed.json")

# 部署相关默认值集中于此（仅在 config.json 缺失对应字段时使用）
DEFAULT_STREAM_PREFIX = "http://23.19.231.70:5244/d/115"
DEFAULT_CD2_MOUNT_PATH = "/Movies/CloudDrive/115"
DEFAULT_OUTPUT_DIR = "/Movies/TreeStrms"
DEFAULT_ADULT_CID = "3291659674416491425"
DEFAULT_MOVIES_CID = "2557717596187131421"
DEFAULT_TPDB_BASE = "https://api.theporndb.net"

DEFAULT_PROTECTED_CATEGORIES = ["欧美刮削", "欧美刮削_01", "AV女优", "FC2", "我爱的AV", "无码刮削", "AdultMovie"]

COMPREHENSIVE_VIDEO_EXTS = {
    '.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v',
    '.ts', '.m2ts', '.mts', '.vob', '.iso', '.rmvb', '.rm', '.asf',
    '.264', '.265', '.hevc', '.mpg', '.mpeg', '.f4v'
}

COMPREHENSIVE_SUBTITLE_EXTS = {
    '.srt', '.ass', '.ssa', '.vtt', '.sub', '.sup', '.idx', '.smi'
}

def default_ai_governance() -> Dict[str, Any]:
    return {
        "enabled": True,
        "api_base": "https://openrouter.ai/api/v1",
        "api_key": "",
        "model": "minimax/minimax-m2.7:free",
        "batch_size": 50
    }

class AIGovModel(BaseModel):
    enabled: bool = True
    api_base: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    model: str = "minimax/minimax-m2.7:free"
    batch_size: int = 100

class ConfigModel(BaseModel):
    cookie: str
    default_prefix: str
    cd2_mount_path: Optional[str] = DEFAULT_CD2_MOUNT_PATH
    default_output: str
    layer_limit: int = 25
    sync_subtitles: bool = True
    auto_cleanup_orphan: bool = True
    cover_threads: int = 3
    ai_governance: AIGovModel
    auto_sync_enabled: bool
    auto_sync_interval_mins: int
    # rules / protected_categories 由各自接口维护，不随全局配置保存
    # 以下字段为 None 表示前端未提交，保持原值
    tpdb_base: Optional[str] = None
    tpdb_token: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

class ConfigLoadError(RuntimeError):
    """config.json 存在但无法解析且无可用备份。宁可报错停服，也绝不用默认配置覆盖用户配置。"""

# RLock：modify_config 持锁期间会调用 save_config
_config_write_lock = threading.RLock()

def load_cookie_from_env() -> str:
    env_p = "/opt/115-agent/.env"
    if os.path.exists(env_p):
        with open(env_p, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("PAN115_COOKIE="):
                    return line.strip().split("=", 1)[1]
    return ""

def _read_json_dict(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("顶层不是 JSON 对象")
    return data

def _apply_runtime_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """仅在内存中补齐缺省字段，不写盘"""
    if not cfg.get("cookie"):
        cfg["cookie"] = load_cookie_from_env()
    cfg.setdefault("protected_categories", list(DEFAULT_PROTECTED_CATEGORIES))
    cfg.setdefault("ai_governance", default_ai_governance())
    cfg.setdefault("cover_threads", 3)
    cfg.setdefault("cd2_mount_path", DEFAULT_CD2_MOUNT_PATH)
    return cfg

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            return _apply_runtime_defaults(_read_json_dict(CONFIG_FILE))
        except (OSError, ValueError) as exc:
            # 绝不能落入下方默认配置分支：那会 save_config 覆盖掉用户的 cookie / rules / access_token
            if os.path.exists(CONFIG_BACKUP_FILE):
                try:
                    cfg = _read_json_dict(CONFIG_BACKUP_FILE)
                    print(f"[TreeSTRM] ⚠️ {CONFIG_FILE} 解析失败（{exc}），已改用备份 {CONFIG_BACKUP_FILE}；下次保存配置时将自动修复主文件", file=sys.stderr, flush=True)
                    return _apply_runtime_defaults(cfg)
                except (OSError, ValueError):
                    pass
            raise ConfigLoadError(f"{CONFIG_FILE} 解析失败且无可用备份，请手动修复：{exc}") from exc

    cfg = {
        "cookie": load_cookie_from_env(),
        "default_prefix": DEFAULT_STREAM_PREFIX,
        "cd2_mount_path": DEFAULT_CD2_MOUNT_PATH,
        "default_output": DEFAULT_OUTPUT_DIR,
        "layer_limit": 25,
        "sync_subtitles": True,
        "auto_cleanup_orphan": True,
        "cover_threads": 3,
        "protected_categories": list(DEFAULT_PROTECTED_CATEGORIES),
        "ai_governance": default_ai_governance(),
        "auto_sync_enabled": False,
        "auto_sync_interval_mins": 30,
        "rules": [
            {
                "id": "chengren_all",
                "name": "成人",
                "cid": DEFAULT_ADULT_CID,
                "output_dir": DEFAULT_OUTPUT_DIR,
                "prefix": DEFAULT_STREAM_PREFIX,
                "sync_subtitles": True,
                "auto_cleanup_orphan": True,
                "enabled": True,
                "last_sync": None,
                "last_status": "就绪",
                "video_count": 0,
                "sub_count": 0,
                "ext_stats": {}
            }
        ]
    }
    save_config(cfg)
    return cfg

def save_config(cfg: dict):
    """原子写入：先写临时文件再 os.replace，写入前把当前（有效的）主文件备份为 .bak"""
    with _config_write_lock:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        tmp_path = CONFIG_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(CONFIG_FILE):
            try:
                _read_json_dict(CONFIG_FILE)
                # 只备份有效主文件，避免用损坏文件覆盖好的备份
                shutil.copy2(CONFIG_FILE, CONFIG_BACKUP_FILE)
            except (OSError, ValueError):
                pass
        os.replace(tmp_path, CONFIG_FILE)

def modify_config(mutator: Callable[[dict], None]) -> dict:
    """读-改-写整体加锁，避免多线程各自 load/save 时后写者覆盖前者的修改（丢失更新）"""
    with _config_write_lock:
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
        return cfg
