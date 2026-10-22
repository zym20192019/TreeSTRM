"""Getchu：二次元里番（按标题检索，结果靠标题相似度判定）"""

from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import quote

from ..models import KIND_HENTAI, MediaInfo, Metadata
from .base import Source, abs_url, clean_text, dedupe, parse_date, soup_of

BASE = "https://www.getchu.com"
HEADERS = {"Cookie": "getchu_adalt_flag=getchu.com; gc=gc"}


class Getchu(Source):
    name = "getchu"
    label = "Getchu"
    kinds = frozenset({KIND_HENTAI})
    probe_url = BASE + "/"

    def _search(self, keyword: str, limit: int) -> List[Metadata]:
        # Getchu 页面是 EUC-JP，关键词也必须按 EUC-JP 编码
        try:
            encoded = quote(keyword.encode("euc_jp", errors="ignore"))
        except LookupError:
            encoded = quote(keyword)
        r = self.get(f"{BASE}/php/nsearch.phtml?search_keyword={encoded}&gc=gc", headers=HEADERS)
        if r.status != 200:
            return []
        out, seen = [], set()
        for a in soup_of(r.text).find_all("a", href=re.compile(r'soft\.phtml\?id=\d+')):
            m = re.search(r'id=(\d+)', a["href"])
            title = clean_text(a.get_text())
            if not m or not title or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            out.append(Metadata(source=self.name, title=title, source_id=m.group(1), url=f"{BASE}/soft.phtml?id={m.group(1)}&gc=gc"))
            if len(out) >= limit:
                break
        return out

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        keyword = info.title_hint or info.number
        if not keyword:
            return []
        results = []
        for item in self._search(keyword, 3):
            detail = self.fetch_by_id(item.source_id)
            if detail:
                results.append(detail)
        return results

    def search(self, query: str, info: MediaInfo) -> List[Metadata]:
        return self._search(query, 10)

    def fetch_by_id(self, source_id: str) -> Optional[Metadata]:
        if not source_id.isdigit():
            return None
        url = f"{BASE}/soft.phtml?id={source_id}&gc=gc"
        r = self.get(url, headers=HEADERS)
        if r.status != 200:
            return None
        soup = soup_of(r.text)
        title_tag = soup.select_one("h1#soft-title") or soup.find("h1")
        title = clean_text(title_tag.get_text()) if title_tag else ""
        title = re.sub(r'\s*（.*?）\s*$', '', title)
        if not title:
            return None
        meta = Metadata(source=self.name, title=title, original_title=title, url=url, source_id=source_id, number=title, image_referer=BASE + "/")
        pkg = soup.select_one("a.highslide")
        if pkg and pkg.get("href"):
            meta.poster_url = abs_url(BASE + "/", pkg.get("href"))
        text = soup.get_text(" ")
        m = re.search(r'発売日\s*[:：]?\s*(\d{4}/\d{2}/\d{2})', text)
        meta.premiered = parse_date(m.group(1)) if m else ""
        brand = soup.select_one('a[href*="search_brand_id"], a.glance')
        meta.studio = clean_text(brand.get_text()) if brand else ""
        meta.genres = dedupe(a.get_text() for a in soup.select('a[href*="genre="]'))[:15]
        story = soup.select_one("div.tablebody")
        meta.plot = clean_text(story.get_text())[:1500] if story else ""
        meta.tags = ["里番"]
        return meta
