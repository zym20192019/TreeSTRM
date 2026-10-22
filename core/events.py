#!/usr/bin/env python3
"""
TreeSTRM 日志与事件广播模块
"""

import time
import json
import os
import requests
from typing import List
from core.config import SPEED_FILE, DATA_DIR

LOG_DIR = os.path.join(DATA_DIR, "logs")

# 全进程唯一的实时日志缓冲区：app.py 与 engine/services 各模块共用，/api/status 读取此处
live_log_messages: List[str] = []
MAX_LIVE_LOGS = 1000

TG_BOT_TOKEN = ""
TG_CHAT_ID = "5662349315"
MIN_SAFE_DELAY = 2.0

def push_log(msg: str):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    live_log_messages.append(entry)
    if len(live_log_messages) > MAX_LIVE_LOGS:
        live_log_messages.pop(0)

    # 7 天持久化滚动存储
    today = time.strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"treestrm_{today}.log")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

def get_live_logs() -> List[str]:
    return list(live_log_messages)

def send_telegram_alert(text: str):
    if not TG_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
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
