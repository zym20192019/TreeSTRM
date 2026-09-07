#!/usr/bin/env python3
"""
TreeSTRM 日志与事件广播模块
"""

import time
import json
import os
import requests
from typing import List
from core.config import SPEED_FILE

live_log_messages: List[str] = []
MAX_LIVE_LOGS = 300

TG_BOT_TOKEN = ""
TG_CHAT_ID = "5662349315"
MIN_SAFE_DELAY = 2.0

def push_log(msg: str):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    live_log_messages.append(entry)
    if len(live_log_messages) > MAX_LIVE_LOGS:
        live_log_messages.pop(0)

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
