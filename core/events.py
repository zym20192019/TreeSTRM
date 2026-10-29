#!/usr/bin/env python3
"""
TreeSTRM 日志与事件广播模块
"""

import time
import json
import os
import glob
import requests
from typing import List, Optional
from core.config import SPEED_FILE, DATA_DIR, load_config

LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_RETENTION_DAYS = 7

# 全进程唯一的实时日志缓冲区：app.py 与 engine/services 各模块共用，/api/status 读取此处
live_log_messages: List[str] = []
MAX_LIVE_LOGS = 1000

# 强制红线：单 Cookie 慢牛流控底线 >= 2.0s
MIN_SAFE_DELAY = 2.0

_last_log_day: Optional[str] = None

def cleanup_old_logs():
    """删除超过 LOG_RETENTION_DAYS 天的历史日志文件"""
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for p in glob.glob(os.path.join(LOG_DIR, "treestrm_*.log")):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass

def push_log(msg: str):
    global _last_log_day
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    live_log_messages.append(entry)
    if len(live_log_messages) > MAX_LIVE_LOGS:
        live_log_messages.pop(0)

    # 7 天持久化滚动存储：进程首条日志及每次跨天时清理过期文件
    today = time.strftime("%Y-%m-%d")
    if today != _last_log_day:
        _last_log_day = today
        cleanup_old_logs()
    log_file = os.path.join(LOG_DIR, f"treestrm_{today}.log")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

def init_load_recent_logs():
    """启动时从最近的日志文件预载实时日志，重启后运行日志页不为空"""
    if live_log_messages:
        return
    lines: List[str] = []
    for p in sorted(glob.glob(os.path.join(LOG_DIR, "treestrm_*.log")))[-2:]:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines.extend(f.read().splitlines())
        except OSError:
            pass
    for line in lines[-MAX_LIVE_LOGS:]:
        # 文件格式 [YYYY-mm-dd HH:MM:SS] msg → 实时格式 [HH:MM:SS] msg
        if len(line) > 21 and line[0] == "[" and line[20] == "]":
            line = f"[{line[12:20]}]{line[21:]}"
        live_log_messages.append(line)


def get_live_logs() -> List[str]:
    return list(live_log_messages)

def send_telegram_alert(text: str):
    """config.json 中同时配置 telegram_bot_token 与 telegram_chat_id 时才推送"""
    try:
        cfg = load_config()
    except Exception:
        return
    bot_token = str(cfg.get("telegram_bot_token") or "").strip()
    chat_id = str(cfg.get("telegram_chat_id") or "").strip()
    if not bot_token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception:
        pass

def get_dynamic_delay() -> float:
    try:
        if os.path.exists(SPEED_FILE):
            with open(SPEED_FILE, "r") as f:
                d = json.load(f)
                val = float(d.get("delay", 2.0))
                return max(MIN_SAFE_DELAY, val)
    except Exception:
        pass
    return 2.0
