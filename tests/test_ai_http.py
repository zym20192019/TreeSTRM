import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.ai_http import post_with_backend_wait


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float):
        self.t += s


def _poster(fail_times: int, exc=requests.exceptions.ConnectionError):
    calls = {"n": 0}

    def post(url, headers, json, timeout):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc("down")
        return "ok"
    return post, calls


def test_waits_for_backend_then_succeeds():
    clock, logs = _Clock(), []
    post, calls = _poster(3)
    r = post_with_backend_wait("http://127.0.0.1:11434/v1/chat/completions", headers={}, json={}, timeout=1,
                               max_wait=60, poll=5, log=logs.append, session_post=post, sleep=clock.sleep, clock=clock)
    assert r == "ok" and calls["n"] == 4 and clock.t == 15
    assert len(logs) == 1 and "127.0.0.1:11434" in logs[0]


def test_gives_up_after_max_wait():
    clock = _Clock()
    post, calls = _poster(10**6)
    try:
        post_with_backend_wait("http://x/v1/chat/completions", headers={}, json={}, timeout=1, max_wait=20, poll=5,
                               session_post=post, sleep=clock.sleep, clock=clock)
    except requests.exceptions.ConnectionError:
        assert calls["n"] == 5 and clock.t == 20
    else:
        raise AssertionError("should raise")


def test_read_timeout_not_retried():
    clock = _Clock()
    post, calls = _poster(1, exc=requests.exceptions.ReadTimeout)
    try:
        post_with_backend_wait("http://x/v1", headers={}, json={}, timeout=1, max_wait=60, poll=5,
                               session_post=post, sleep=clock.sleep, clock=clock)
    except requests.exceptions.ReadTimeout:
        assert calls["n"] == 1 and clock.t == 0
    else:
        raise AssertionError("should raise")


def test_connect_timeout_is_retried():
    clock = _Clock()
    post, calls = _poster(2, exc=requests.exceptions.ConnectTimeout)
    assert post_with_backend_wait("http://x/v1", headers={}, json={}, timeout=1, max_wait=60, poll=5,
                                  session_post=post, sleep=clock.sleep, clock=clock) == "ok"
    assert calls["n"] == 3
