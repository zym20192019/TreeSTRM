"""制作商直连：MGStage、Prestige、FALENO（这三站通常只允许日本 IP，403 时会自动熔断降级）"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ..identify import number_key
from ..models import KIND_JAV, KIND_MGS, Actor, MediaInfo, Metadata
from .base import Source, abs_url, clean_text, dedupe, og, parse_date, parse_runtime, soup_of

PRESTIGE_PREFIXES = {
    "ABW", "ABP", "ABF", "ABS", "AKA", "BGN", "CHN", "DIC", "DOCP", "EDD", "ESK", "FIV", "GETS", "INU", "JBS",
    "MAS", "MBM", "NOP", "OKP", "PPT", "PXH", "RDT", "SGA", "SIM", "SRS", "TDT", "TEM", "WPS", "YRH", "ZZR",
}
FALENO_PREFIXES = {"FSDSS", "FCDSS", "FLNS", "FTHT", "FTKD", "MFCS", "FADSS", "FGAN"}


def _prefix(number: str) -> str:
    m = re.match(r'^\d*([A-Z]+)', (number or "").upper())
    return m.group(1) if m else ""


class MGStage(Source):
    name = "mgstage"
    label = "MGStage"
    kinds = frozenset({KIND_MGS, KIND_JAV})
    probe_url = "https://www.mgstage.com/"
    note = "仅日本 IP 可访问"

    def supports(self, info: MediaInfo) -> bool:
        # 有码番号只对 Prestige 系尝试 MGStage（其余厂牌 MGS 不售卖）
        return info.kind == KIND_MGS or (info.kind == KIND_JAV and _prefix(info.number) in PRESTIGE_PREFIXES)

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        if not info.number:
            return []
        url = f"https://www.mgstage.com/product/product_detail/{quote(info.number)}/"
        r = self.get(url, headers={"Cookie": "adc=1"})
        if r.status != 200 or "detail_data" not in r.text:
            return []
        soup = soup_of(r.text)
        h1 = soup.select_one("h1.tag") or soup.find("h1")
        meta = Metadata(source=self.name, number=info.number, url=url, source_id=info.number, image_referer="https://www.mgstage.com/")
        meta.title = clean_text(h1.get_text()) if h1 else ""
        meta.original_title = meta.title
        enlarge = soup.select_one("a#EnlargeImage") or soup.select_one("a.enlarge_image")
        if enlarge and enlarge.get("href"):
            meta.cover_url = abs_url(url, enlarge.get("href"))
        for tr in soup.select("div.detail_data table tr"):
            th, td = tr.find("th"), tr.find("td")
            if not th or not td:
                continue
            key = clean_text(th.get_text()).rstrip(':：')
            text = clean_text(td.get_text())
            links = [clean_text(a.get_text()) for a in td.find_all("a")]
            if key == "出演":
                meta.actors = [Actor(name=n) for n in dedupe(links or [text])]
            elif key == "メーカー":
                meta.studio = links[0] if links else text
            elif key == "収録時間":
                meta.runtime = parse_runtime(text)
            elif key == "品番":
                meta.number = text or meta.number
            elif key in ("配信開始日", "商品発売日") and not meta.premiered:
                meta.premiered = parse_date(text)
            elif key == "シリーズ":
                meta.series = links[0] if links else text
            elif key == "レーベル":
                meta.label = links[0] if links else text
            elif key == "ジャンル":
                meta.genres = dedupe(links)
        intro = soup.select_one("p.txt.introduction") or soup.select_one("#introduction dd p")
        meta.plot = clean_text(intro.get_text()) if intro else ""
        meta.extrafanart = dedupe(abs_url(url, a.get("href")) for a in soup.select("a.sample_image"))[:20]
        return [meta] if (meta.title or meta.cover_url) else []


class Prestige(Source):
    name = "prestige"
    label = "Prestige 官方"
    kinds = frozenset({KIND_JAV})
    probe_url = "https://www.prestige-av.com/"
    note = "仅日本 IP 可访问"

    def supports(self, info: MediaInfo) -> bool:
        return info.kind == KIND_JAV and _prefix(info.number) in PRESTIGE_PREFIXES

    def _json(self, url: str) -> Optional[Dict[str, Any]]:
        r = self.get(url, headers={"Accept": "application/json"})
        if r.status != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        if not info.number:
            return []
        product = None
        for sku in (info.number.upper(), info.number.upper().replace("-", "")):
            item = self._json(f"https://www.prestige-av.com/api/sku/item/{quote(sku)}")
            uuid = ((item or {}).get("parentProduct") or {}).get("uuid")
            if uuid:
                product = self._json(f"https://www.prestige-av.com/api/product/{uuid}")
                if product:
                    break
        if not product:
            return []

        def image(key: str) -> str:
            path = (product.get(key) or {}).get("path") if isinstance(product.get(key), dict) else ""
            if not path:
                return ""
            return path if path.startswith("http") else f"https://www.prestige-av.com/api/media/{path}"

        def names(key: str) -> List[str]:
            value = product.get(key) or []
            if isinstance(value, dict):
                value = [value]
            return dedupe(v.get("name", "") for v in value if isinstance(v, dict))

        meta = Metadata(
            source=self.name,
            number=info.number,
            title=clean_text(product.get("title")),
            original_title=clean_text(product.get("title")),
            plot=clean_text(product.get("body")),
            studio=(names("maker") or ["Prestige"])[0],
            label=(names("label") or [""])[0],
            series=(names("series") or [""])[0],
            premiered=parse_date(product.get("salesStartAt") or product.get("mgsStartAt") or product.get("deliveryStartAt") or ""),
            runtime=parse_runtime(product.get("playTime") or 0),
            genres=names("genre"),
            actors=[Actor(name=n) for n in names("actress")],
            cover_url=image("packageImage"),
            poster_url=image("thumbnail"),
            extrafanart=dedupe(
                (s.get("path") if s.get("path", "").startswith("http") else f"https://www.prestige-av.com/api/media/{s.get('path')}")
                for s in (product.get("media") or []) if isinstance(s, dict) and s.get("path")
            )[:20],
            url=f"https://www.prestige-av.com/goods/{product.get('uuid', '')}",
            source_id=info.number,
        )
        return [meta] if meta.title else []


class Faleno(Source):
    name = "faleno"
    label = "FALENO 官方"
    kinds = frozenset({KIND_JAV})
    probe_url = "https://faleno.jp/"
    note = "仅日本 IP 可访问"

    def supports(self, info: MediaInfo) -> bool:
        return info.kind == KIND_JAV and _prefix(info.number) in FALENO_PREFIXES

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        if not info.number:
            return []
        r = self.get("https://faleno.jp/top/search", params={"keyword": info.number})
        if r.status != 200:
            return []
        target = number_key(info.number)
        detail_url = ""
        for a in soup_of(r.text).find_all("a", href=True):
            href = a["href"]
            if re.search(r'/(?:works|products/detail)/', href) and number_key(re.sub(r'.*/', '', href.rstrip('/'))) == target:
                detail_url = abs_url("https://faleno.jp/", href)
                break
        if not detail_url:
            return []
        dr = self.get(detail_url)
        if dr.status != 200 or target not in number_key(dr.text[:200000]):
            return []
        soup = soup_of(dr.text)
        title_tag = soup.select_one("h1") or soup.select_one(".p-product-title")
        meta = Metadata(source=self.name, number=info.number, studio="FALENO", url=detail_url, source_id=info.number)
        meta.title = clean_text(title_tag.get_text()) if title_tag else og(soup, "title")
        meta.original_title = meta.title
        meta.cover_url = og(soup, "image")
        meta.actors = [Actor(name=n) for n in dedupe(a.get_text() for a in soup.select('a[href*="/actress/"]'))]
        text = soup.get_text(" ")
        m = re.search(r'(?:配信開始日|発売日)\s*[:：]?\s*([0-9/\-.年月日]{8,12})', text)
        meta.premiered = parse_date(m.group(1)) if m else ""
        m = re.search(r'収録時間\s*[:：]?\s*(\d+)\s*分', text)
        meta.runtime = int(m.group(1)) if m else 0
        desc = soup.select_one(".box_works01_text p") or soup.find("meta", attrs={"name": "description"})
        meta.plot = clean_text(desc.get("content") if desc and desc.name == "meta" else (desc.get_text() if desc else ""))
        return [meta] if meta.title else []
