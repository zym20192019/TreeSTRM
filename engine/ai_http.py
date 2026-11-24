#!/usr/bin/env python3
"""
AI 接口 HTTP 调用：连不上后端时先等它恢复再重试。

本机 Ollama 经 SSH 反向隧道给 23 用，隧道断线几秒后会自动重连；以前一连不上 2 秒内就判失败，
NFO 重写会连跳一串目录（11-23 01:38 断 2 秒丢了 6 个目录）。这里只对「连接失败/连接超时」等待，
读超时、HTTP 错误、内容错误照常抛给调用方按原有逻辑重试/降级。
"""

import time
from typing import Callable, Optional

import requests

BACKEND_MAX_WAIT = 180  # 秒
BACKEND_POLL = 5  # 秒


def post_with_backend_wait(url: str, *, headers: dict, json: dict, timeout, max_wait: float = BACKEND_MAX_WAIT,
                           poll: float = BACKEND_POLL, log: Optional[Callable[[str], None]] = None,
                           session_post: Callable = requests.post, sleep: Callable[[float], None] = time.sleep,
                           clock: Callable[[], float] = time.monotonic) -> requests.Response:
    """requests.post；ConnectionError（含 ConnectTimeout、请求中途断开）时每 poll 秒重试，最多等 max_wait 秒。"""
    deadline = clock() + max_wait
    warned = False
    while True:
        try:
            return session_post(url, headers=headers, json=json, timeout=timeout)
        except requests.exceptions.ConnectionError:
            if clock() >= deadline:
                raise
            if not warned and log is not None:
                log(f"🔌 AI 后端连接失败，等待恢复（最多 {int(max_wait)} 秒）: {url.split('/v1')[0]}")
                warned = True
            sleep(poll)
