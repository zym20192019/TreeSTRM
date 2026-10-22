"""JavDB：先搜索，番号完全一致才进入详情页（旧版直接取第一条结果，会挂错封面）"""

from __future__ import annotations

import re
from typing import List, Optional

from ..identify import mgs_alias, numbers_match
from ..models import KIND_FC2, KIND_JAV, KIND_MGS, KIND_UNCENSORED, Actor, MediaInfo, Metadata
from .base import Source, abs_url, clean_text, dedupe, parse_date, parse_runtime, soup_of

BASE = "https://javdb.com"
HEADERS = {"Cookie": "over18=1; locale=zh"}


class JavDB(Source):
    name = "javdb"
    label = "JavDB"
    kinds = frozenset({KIND_JAV, KIND_MGS, KIND_UNCENSORED, KIND_FC2})
    probe_url = BASE + "/"

    def _search(self, query: str) -> List[Metadata]:
        r = self.get(f"{BASE}/search", params={"q": query, "f": "all"}, headers=HEADERS)
        if r.status != 200:
            return []
        out = []
        for item in soup_of(r.text).select("div.movie-list div.item"):
            a = item.find("a", class_="box")
            title_div = item.find("div", class_="video-title")
            if not a or not title_div:
                continue
            strong = title_div.find("strong")
            number = clean_text(strong.get_text()) if strong else ""
            img = item.find("img")
            meta = Metadata(
                source=self.name,
                number=number,
                title=clean_text(a.get("title")) or clean_text(title_div.get_text().replace(number, "", 1)),
                premiered=parse_date(clean_text(item.find("div", class_="meta").get_text()) if item.find("div", class_="meta") else ""),
                cover_url=abs_url(BASE, img.get("src")) if img else "",
                url=abs_url(BASE, a.get("href")),
                source_id=a.get("href") or "",
            )
            out.append(meta)
        return out

    def _query_for(self, info: MediaInfo) -> str:
        if info.kind == KIND_FC2:
            return info.number.replace("FC2-PPV-", "FC2-")
        return info.number

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        if not info.number:
            return []
        queries = [self._query_for(info)] + [n for n in [mgs_alias(info.number)] if n]
        for query in queries:
            for item in self._search(query):
                if numbers_match(item.number, info.number):
                    detail = self.fetch_by_id(item.source_id)
                    return [detail] if detail else [item]
        return []

    def search(self, query: str, info: MediaInfo) -> List[Metadata]:
        return self._search(query)[:10]

    def fetch_by_id(self, source_id: str) -> Optional[Metadata]:
        if not source_id.startswith("/v/"):
            return None
        r = self.get(BASE + source_id, headers=HEADERS)
        if r.status != 200:
            return None
        soup = soup_of(r.text)
        h2 = soup.find("h2")
        if not h2:
            return None
        meta = Metadata(source=self.name, url=r.url, source_id=source_id, image_referer=BASE + "/")
        first = h2.find("strong")
        meta.number = clean_text(first.get_text()) if first else ""
        origin = h2.find("span", class_="origin-title")
        current = h2.find("strong", class_="current-title")
        # JavDB 的中文标题由用户翻译，常有张冠李戴，优先用原标题
        meta.title = clean_text((origin or current).get_text()) if (origin or current) else ""
        meta.original_title = meta.title
        cover = soup.select_one("img.video-cover")
        meta.cover_url = abs_url(BASE, cover.get("src")) if cover else ""

        for block in soup.select("nav.movie-panel-info div.panel-block"):
            key_tag = block.find("strong")
            value = block.find("span", class_="value")
            if not key_tag or not value:
                continue
            key = clean_text(key_tag.get_text()).rstrip(':：')
            text = clean_text(value.get_text())
            if key in ("番號", "番号", "ID"):
                meta.number = re.sub(r'\s+', '', text) or meta.number
            elif key in ("日期", "Released Date"):
                meta.premiered = parse_date(text)
            elif key in ("時長", "时长", "Duration"):
                meta.runtime = parse_runtime(text)
            elif key in ("導演", "导演", "Director"):
                meta.director = text
            elif key in ("片商", "Maker"):
                meta.studio = text
            elif key in ("發行", "发行", "Publisher"):
                meta.label = text
            elif key in ("系列", "Series"):
                meta.series = text
            elif key in ("評分", "评分", "Rating"):
                m = re.search(r'([\d.]+)\s*分', text)
                if m:
                    meta.rating = round(min(5.0, float(m.group(1))) * 2, 1)
            elif key in ("類別", "类别", "Tags"):
                meta.genres = dedupe(a.get_text() for a in value.find_all("a"))
            elif key in ("演員", "演员", "Actor(s)"):
                links = value.find_all("a")
                female = [a for a in links if "actor-female" in (a.get("class") or [])]
                # 站点标注了性别时只取女优
                chosen = female or links
                meta.actors = [Actor(name=clean_text(a.get_text())) for a in chosen if clean_text(a.get_text())]

        meta.extrafanart = dedupe(
            abs_url(BASE, a.get("href")) for a in soup.select("div.preview-images a.tile-item")
            if (a.get("href") or "").lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        )[:20]
        return meta
