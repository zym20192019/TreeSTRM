"""JavBus 与同构站 AVSOX"""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote

from ..identify import mgs_alias, numbers_match
from ..models import KIND_JAV, KIND_MGS, KIND_UNCENSORED, Actor, MediaInfo, Metadata
from .base import Source, abs_url, clean_text, dedupe, is_placeholder_image, parse_date, parse_runtime, soup_of, strip_number_prefix

JAVBUS_BASE = "https://www.javbus.com"
# 未带 age=verified 时会被重定向到 /doc/driver-verify
JAVBUS_HEADERS = {"Cookie": "age=verified; dv=1; existmag=all"}


def parse_javbus_detail(html: str, page_url: str, base: str, source: str) -> Optional[Metadata]:
    """JavBus / AVSOX 详情页结构相同：a.bigImage + div.info"""
    soup = soup_of(html)
    big = soup.select_one("a.bigImage")
    info = soup.select_one("div.info")
    if not big or not info:
        return None
    meta = Metadata(source=source, url=page_url)
    cover = abs_url(base, big.get("href") or (big.img.get("src") if big.img else ""))
    if not is_placeholder_image(cover):
        meta.cover_url = cover
    meta.image_referer = base + "/"

    for p in info.find_all("p"):
        header = p.find("span", class_="header")
        if not header:
            continue
        key = clean_text(header.get_text()).rstrip(':：')
        value_text = clean_text(p.get_text().replace(header.get_text(), "", 1))
        link = p.find("a")
        link_text = clean_text(link.get_text()) if link else ""
        if key in ("識別碼", "识别码", "品番", "ID"):
            meta.number = value_text
        elif key in ("發行日期", "发行日期", "發行時間", "发行时间", "Release Date"):
            meta.premiered = parse_date(value_text)
        elif key in ("長度", "长度", "Length"):
            meta.runtime = parse_runtime(value_text)
        elif key in ("導演", "导演", "Director"):
            meta.director = link_text or value_text
        elif key in ("製作商", "制作商", "Studio"):
            meta.studio = link_text or value_text
        elif key in ("發行商", "发行商", "Label"):
            meta.label = link_text or value_text
        elif key in ("系列", "Series"):
            meta.series = link_text or value_text

    meta.genres = dedupe(a.get_text() for a in info.select('span.genre a[href*="/genre/"]'))

    thumbs = {}
    for box in soup.select("a.avatar-box, div.star-box"):
        img = box.find("img")
        if img and img.get("title"):
            src = abs_url(base, img.get("src"))
            thumbs[clean_text(img.get("title"))] = "" if is_placeholder_image(src) else src
    names = [a.get_text() for a in info.select("div.star-name a")] or list(thumbs.keys())
    if not names:
        names = [s.get_text() for s in soup.select("a.avatar-box span")]
    meta.actors = [Actor(name=n, thumb=thumbs.get(n, "")) for n in dedupe(names)]

    meta.extrafanart = dedupe(abs_url(base, a.get("href")) for a in soup.select("#sample-waterfall a.sample-box"))[:20]

    title = soup.find("h3")
    meta.title = strip_number_prefix(clean_text(title.get_text()) if title else "", meta.number)
    # 标题末尾常拼接演员名
    stripped = True
    while stripped:
        stripped = False
        for actor in meta.actors:
            if actor.name and meta.title.endswith(" " + actor.name):
                meta.title = meta.title[: -len(actor.name) - 1].rstrip()
                stripped = True
    meta.original_title = meta.title
    return meta if (meta.title or meta.cover_url) else None


class JavBus(Source):
    name = "javbus"
    label = "JavBus"
    kinds = frozenset({KIND_JAV, KIND_MGS, KIND_UNCENSORED})
    probe_url = JAVBUS_BASE + "/"

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        if not info.number:
            return []
        for number in [info.number] + [n for n in [mgs_alias(info.number)] if n]:
            r = self.get(f"{JAVBUS_BASE}/{quote(number)}", headers=JAVBUS_HEADERS)
            if r.status != 200 or "driver-verify" in r.url:
                continue
            meta = parse_javbus_detail(r.text, r.url, JAVBUS_BASE, self.name)
            if meta:
                meta.source_id = number
                return [meta]
        return []

    def fetch_by_id(self, source_id: str) -> Optional[Metadata]:
        r = self.get(f"{JAVBUS_BASE}/{quote(source_id)}", headers=JAVBUS_HEADERS)
        if r.status != 200:
            return None
        meta = parse_javbus_detail(r.text, r.url, JAVBUS_BASE, self.name)
        if meta:
            meta.source_id = source_id
        return meta


class Avsox(Source):
    name = "avsox"
    label = "AVSOX"
    kinds = frozenset({KIND_JAV, KIND_UNCENSORED})
    base = "https://avsox.host"
    probe_url = "https://avsox.host/cn"

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        if not info.number:
            return []
        r = self.get(f"{self.base}/cn/search/{quote(info.number)}")
        if r.status != 200:
            return []
        soup = soup_of(r.text)
        for box in soup.select("a.movie-box"):
            dates = [clean_text(d.get_text()) for d in box.find_all("date")]
            if dates and numbers_match(dates[0], info.number):
                detail_url = abs_url(self.base, box.get("href"))
                return [m for m in [self._detail(detail_url)] if m]
        return []

    def _detail(self, url: str) -> Optional[Metadata]:
        r = self.get(url)
        if r.status != 200:
            return None
        meta = parse_javbus_detail(r.text, r.url, self.base, self.name)
        if meta:
            meta.source_id = url
        return meta

    def fetch_by_id(self, source_id: str) -> Optional[Metadata]:
        return self._detail(source_id) if source_id.startswith(self.base + "/") else None
