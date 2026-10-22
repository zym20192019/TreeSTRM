"""ThePornDB：欧美 Scene。先把站点名解析成 TPDB 站点 slug，再按「站点+日期」「站点+演员」逐级检索"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional

from ..models import KIND_WESTERN, Actor, MediaInfo, Metadata
from .base import Source, clean_text, dedupe

# 内置兜底 Token：config.json 未配置 tpdb_token 时使用
BUILTIN_TOKEN = "GYJTmJLvxL1XKiQ3XSJhImg9fWb3C6yVztbNyp8pf1cd2f25"
DEFAULT_BASE = "https://api.theporndb.net"

# 文件名里的常见缩写 → TPDB 站点名（只做缩写展开，不再把不同站点合并成一个）
SITE_ALIASES = {
    "tsma": "teensexmania", "itc": "inthecrack", "gff": "girlfriendsfilms", "lcd": "littlecaprice",
    "dpg": "digitalplayground", "jlmf": "jeshbyjesh", "bb": "bangbros", "rk": "realitykings",
    "bz": "brazzers", "nf": "nubilesfilms", "pf": "puremature", "mf": "mommysgirl",
    "hart": "hegre", "tushyraw": "tushyraw", "blackedraw": "blackedraw",
}


def norm(value: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (value or "").lower())


class ThePornDB(Source):
    name = "tpdb"
    label = "ThePornDB"
    kinds = frozenset({KIND_WESTERN})
    probe_url = DEFAULT_BASE + "/sites?q=blacked"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._site_cache: Dict[str, Optional[str]] = {}
        self._site_lock = threading.Lock()

    def _api(self) -> tuple:
        cfg = self.ctx.settings()
        token = str(cfg.get("tpdb_token") or "").strip() or BUILTIN_TOKEN
        base = (str(cfg.get("tpdb_base") or "").strip() or DEFAULT_BASE).rstrip("/")
        return base, {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _query(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        base, headers = self._api()
        r = self.get(f"{base}/{path}", params=params, headers=headers, timeout_s=25)
        if r.status != 200:
            return []
        try:
            data = r.json().get("data")
        except (ValueError, AttributeError):
            return []
        if isinstance(data, dict):
            return [data]
        return [d for d in data or [] if isinstance(d, dict)]

    def site_slug(self, site: str) -> Optional[str]:
        """站点名 → TPDB short_name；名称规范化后必须完全一致（含缩写展开）"""
        key = norm(site)
        if not key:
            return None
        key = SITE_ALIASES.get(key, key)
        with self._site_lock:
            if key in self._site_cache:
                return self._site_cache[key]
        slug = None
        for item in self._query("sites", {"q": site, "per_page": 25}):
            if key in (norm(item.get("name", "")), norm(item.get("short_name", ""))):
                slug = item.get("short_name") or None
                break
        with self._site_lock:
            self._site_cache[key] = slug
        return slug

    def to_meta(self, it: Dict[str, Any]) -> Metadata:
        def first_url(*values: Any) -> str:
            for v in values:
                if isinstance(v, str) and v.startswith("http") and not v.endswith("/"):
                    return v
            return ""

        posters = it.get("posters") or {}
        background = it.get("background") or {}
        performers = []
        for p in it.get("performers") or []:
            parent = p.get("parent") or {}
            gender = ((parent.get("extras") or {}).get("gender") or (p.get("extra") or {}).get("gender") or "")
            performers.append((Actor(
                name=clean_text(parent.get("name") or p.get("name")),
                thumb=first_url(parent.get("image"), p.get("face"), p.get("image")),
            ), str(gender).lower()))
        female = [a for a, g in performers if g == "female"]
        # 有性别信息时只保留女演员，男演员放在 role 里不展示
        actors = female or [a for a, _ in performers]
        duration = it.get("duration") or 0
        site = it.get("site") or {}
        network = site.get("network") or {}
        return Metadata(
            source=self.name,
            title=clean_text(it.get("title")),
            original_title=clean_text(it.get("title")),
            plot=clean_text(it.get("description")),
            studio=clean_text(site.get("name")),
            label=clean_text(network.get("name") if isinstance(network, dict) else ""),
            director=", ".join(dedupe(d.get("name", "") for d in it.get("directors") or [] if isinstance(d, dict))),
            premiered=clean_text(it.get("date"))[:10],
            runtime=int(duration) // 60 if isinstance(duration, (int, float)) and duration >= 60 else 0,
            tags=dedupe(t.get("name") if isinstance(t, dict) else str(t) for t in it.get("tags") or [])[:30],
            actors=actors,
            poster_url=first_url(posters.get("large"), posters.get("full"), it.get("poster")),
            fanart_url=first_url(background.get("full"), background.get("large"), it.get("image")),
            url=clean_text(it.get("url")),
            source_id=str(it.get("id") or ""),
        )

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        seen: Dict[str, Dict[str, Any]] = {}

        def collect(items: List[Dict[str, Any]]) -> None:
            for it in items:
                key = str(it.get("id") or it.get("_id") or "")
                if key and key not in seen:
                    seen[key] = it

        slug = self.site_slug(info.site) if info.site else None
        if slug and info.date:
            collect(self._query("scenes", {"site": slug, "date": info.date, "per_page": 10}))
        if slug and info.title_hint and len(seen) < 3:
            collect(self._query("scenes", {"site": slug, "q": info.title_hint, "per_page": 10}))
        if not seen and info.raw:
            collect(self._query("scenes", {"parse": info.raw, "per_page": 10}))
        if not seen and info.title_hint:
            q = f"{info.site} {info.title_hint}".strip() if not slug else info.title_hint
            collect(self._query("scenes", {"q": q, "per_page": 10}))
        return [self.to_meta(it) for it in seen.values()]

    def search(self, query: str, info: MediaInfo) -> List[Metadata]:
        return [self.to_meta(it) for it in self._query("scenes", {"q": query, "per_page": 15})]

    def fetch_by_id(self, source_id: str) -> Optional[Metadata]:
        items = self._query(f"scenes/{source_id}", {})
        return self.to_meta(items[0]) if items else None
