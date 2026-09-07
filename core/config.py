#!/usr/bin/env python3
"""
TreeSTRM 核心配置管理模块
"""

import os
import json
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

BASE_DIR = "/opt/treestrm"
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_FILE = os.path.join(DATA_DIR, "history.json")
SPEED_FILE = os.path.join(DATA_DIR, "speed.json")

DEFAULT_PROTECTED_CATEGORIES = [
    "欧美刮削_01", "我爱的AV", "无码刮削", "AdultMovie",
    "有声小说", "欧美刮削", "AV女优", "FC2"
]

COMPREHENSIVE_VIDEO_EXTS = {
    '.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v',
    '.ts', '.m2ts', '.mts', '.vob', '.iso', '.rmvb', '.rm', '.asf',
    '.264', '.265', '.hevc', '.mpg', '.mpeg', '.f4v'
}

COMPREHENSIVE_SUBTITLE_EXTS = {
    '.srt', '.ass', '.ssa', '.vtt', '.sub', '.sup', '.idx', '.smi'
}

class RuleModel(BaseModel):
    id: Optional[str] = None
    name: str
    cid: str
    output_dir: Optional[str] = None
    prefix: Optional[str] = None
    sync_subtitles: bool = True
    auto_cleanup_orphan: bool = True
    enabled: bool = True

class AIGovModel(BaseModel):
    enabled: bool = True
    api_base: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    model: str = "minimax/minimax-m2.7:free"
    batch_size: int = 100

class ConfigModel(BaseModel):
    cookie: str
    default_prefix: str = "/movies/CloudDrive/115"
    default_output: str = "/Movies/TreeStrms"
    layer_limit: int = 25
    sync_subtitles: bool = True
    auto_cleanup_orphan: bool = True
    cover_threads: int = 2
    auto_sync_enabled: bool = False
    auto_sync_interval_mins: int = 30
    ai_governance: Optional[AIGovModel] = None

def load_cookie_from_env() -> str:
    env_p = "/opt/115-agent/.env"
    if os.path.exists(env_p):
        with open(env_p, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("PAN115_COOKIE="):
                    return line.strip().split("=", 1)[1]
    return ""

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if not cfg.get("cookie"):
                    cfg["cookie"] = load_cookie_from_env()
                if "protected_categories" not in cfg:
                    cfg["protected_categories"] = DEFAULT_PROTECTED_CATEGORIES
                if "cover_threads" not in cfg:
                    cfg["cover_threads"] = 2
                return cfg
        except Exception:
            pass
    cfg = {
        "cookie": load_cookie_from_env(),
        "default_prefix": "/movies/CloudDrive/115",
        "default_output": "/Movies/TreeStrms",
        "layer_limit": 25,
        "sync_subtitles": True,
        "auto_cleanup_orphan": True,
        "cover_threads": 2,
        "protected_categories": DEFAULT_PROTECTED_CATEGORIES,
        "auto_sync_enabled": False,
        "auto_sync_interval_mins": 30,
        "rules": [
            {
                "id": "chengren_all",
                "name": "成人",
                "cid": "3291659674416491425",
                "output_dir": "/Movies/TreeStrms",
                "prefix": "/movies/CloudDrive/115",
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
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
