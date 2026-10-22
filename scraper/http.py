"""刮削网络层：按站点限速、失败重试、熔断降级、磁盘缓存"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
}

# 连续失败 FAIL_THRESHOLD 次后熔断，冷却时间从 COOLDOWN_BASE_S 起翻倍，上限 COOLDOWN_MAX_S
FAIL_THRESHOLD = 3
COOLDOWN_BASE_S = 600
COOLDOWN_MAX_S = 7200
NOT_FOUND_CACHE_S = 86400

_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")


@dataclass
class Response:
    status: int
    url: str
    text: str
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return self.status == 200


@dataclass
class SiteHealth:
    requests: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    cooldown_s: float = COOLDOWN_BASE_S
    last_error: str = ""
    last_ok_at: float = 0.0
    last_error_at: float = 0.0
    cache_hits: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "requests": self.requests,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "cooling": self.cooldown_until > now,
            "cooldown_left_s": max(0, int(self.cooldown_until - now)),
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
            "last_error_at": self.last_error_at,
            "cache_hits": self.cache_hits,
        }


class SourceUnavailable(Exception):
    """站点处于熔断冷却中"""


class Fetcher:
    def __init__(self, cache_dir: str, *, min_interval_s: float = 1.5, cache_ttl_s: float = 7 * 86400, timeout_s: float = 15):
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.cache_ttl_s = cache_ttl_s
        self.timeout_s = timeout_s
        self._health: Dict[str, SiteHealth] = {}
        self._host_last: Dict[str, float] = {}
        self._host_locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    # ---------- 配置 ----------
    def configure(self, *, min_interval_s: Optional[float] = None, cache_ttl_s: Optional[float] = None) -> None:
        if min_interval_s is not None:
            self.min_interval_s = max(0.3, float(min_interval_s))
        if cache_ttl_s is not None:
            self.cache_ttl_s = max(0.0, float(cache_ttl_s))

    def _session(self) -> requests.Session:
        # requests.Session 非线程安全，每个线程一个
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update(DEFAULT_HEADERS)
            self._local.session = sess
        return sess

    # ---------- 健康度 ----------
    def health(self, source: str) -> SiteHealth:
        with self._lock:
            return self._health.setdefault(source, SiteHealth())

    def health_snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {name: h.snapshot() for name, h in self._health.items()}

    def is_available(self, source: str) -> bool:
        return self.health(source).cooldown_until <= time.time()

    def reset_health(self, source: Optional[str] = None) -> None:
        with self._lock:
            if source:
                self._health.pop(source, None)
            else:
                self._health.clear()

    def _record_ok(self, source: str) -> None:
        h = self.health(source)
        with self._lock:
            h.consecutive_failures = 0
            h.cooldown_s = COOLDOWN_BASE_S
            h.last_ok_at = time.time()

    def _record_failure(self, source: str, error: str) -> None:
        h = self.health(source)
        with self._lock:
            h.failures += 1
            h.consecutive_failures += 1
            h.last_error = error[:300]
            h.last_error_at = time.time()
            if h.consecutive_failures >= FAIL_THRESHOLD:
                h.cooldown_until = time.time() + h.cooldown_s
                h.cooldown_s = min(COOLDOWN_MAX_S, h.cooldown_s * 2)

    # ---------- 限速 ----------
    def _throttle(self, host: str) -> None:
        with self._lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            wait = self._host_last.get(host, 0.0) + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._host_last[host] = time.monotonic()

    # ---------- 缓存 ----------
    def _cache_path(self, key: str) -> str:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, digest[:2], digest + ".json")

    def _cache_get(self, key: str) -> Optional[Response]:
        if self.cache_ttl_s <= 0:
            return None
        path = self._cache_path(key)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        ttl = NOT_FOUND_CACHE_S if data.get("status") == 404 else self.cache_ttl_s
        if time.time() - float(data.get("at", 0)) > ttl:
            return None
        return Response(int(data["status"]), data.get("url", ""), data.get("text", ""), from_cache=True)

    def _cache_put(self, key: str, resp: Response) -> None:
        if self.cache_ttl_s <= 0 or resp.status not in (200, 404):
            return
        path = self._cache_path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"status": resp.status, "url": resp.url, "text": resp.text, "at": time.time()}, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

    def clear_cache(self) -> int:
        removed = 0
        for root, _, files in os.walk(self.cache_dir):
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                    removed += 1
                except OSError:
                    pass
        return removed

    # ---------- 请求 ----------
    @staticmethod
    def _decode(resp: requests.Response) -> str:
        enc = (resp.encoding or "").lower()
        if not enc or enc == "iso-8859-1":
            m = re.search(rb'<meta[^>]+charset=["\']?([A-Za-z0-9_\-]+)', resp.content[:4096], re.IGNORECASE)
            enc = m.group(1).decode("ascii", "ignore") if m else (resp.apparent_encoding or "utf-8")
        try:
            return resp.content.decode(enc, errors="replace")
        except LookupError:
            return resp.content.decode("utf-8", errors="replace")

    def get(
        self,
        source: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        cache: bool = True,
        timeout_s: Optional[float] = None,
        retries: int = 1,
    ) -> Response:
        """GET 请求。站点熔断时抛 SourceUnavailable；网络失败抛 requests 异常"""
        if not self.is_available(source):
            raise SourceUnavailable(source)
        full_url = url + ("?" + urlencode(params) if params else "")
        cache_key = full_url + "|" + json.dumps(headers or {}, sort_keys=True)
        if cache:
            hit = self._cache_get(cache_key)
            if hit is not None:
                self.health(source).cache_hits += 1
                return hit

        host = urlparse(url).netloc
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            self._throttle(host)
            self.health(source).requests += 1
            try:
                r = self._session().get(url, params=params, headers=headers, timeout=timeout_s or self.timeout_s, allow_redirects=True)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                self._record_failure(source, f"{type(exc).__name__}: {exc}")
                raise
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            resp = Response(r.status_code, r.url, self._decode(r))
            if r.status_code in (200, 404):
                self._record_ok(source)
            else:
                # 403 多为地区限制 / 反爬，连续出现后熔断，避免整批请求都白等
                self._record_failure(source, f"HTTP {r.status_code}")
            if cache:
                self._cache_put(cache_key, resp)
            return resp
        raise last_exc or RuntimeError("unreachable")

    def download_image(self, url: str, *, referer: str = "", timeout_s: float = 30, min_bytes: int = 2048) -> Optional[bytes]:
        """下载图片并校验确实是图片（防止把 403 页面 / 占位图写成海报）"""
        if not url or not url.startswith("http"):
            return None
        headers = {"Referer": referer} if referer else None
        host = urlparse(url).netloc
        for attempt in range(2):
            self._throttle(host)
            try:
                r = self._session().get(url, headers=headers, timeout=timeout_s)
            except requests.RequestException:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None
            if r.status_code != 200:
                if r.status_code >= 500 and attempt == 0:
                    time.sleep(2)
                    continue
                return None
            data = r.content
            if len(data) < min_bytes or not data.startswith(_IMAGE_MAGIC):
                return None
            return data
        return None
