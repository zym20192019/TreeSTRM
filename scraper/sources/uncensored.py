"""无码厂牌官方站：加勒比（含 PR）、一本道系（一本道 / 天然むすめ / パコパコママ / ムラムラ）、HEYZO"""

from __future__ import annotations

import re
from typing import List

from ..models import KIND_UNCENSORED, Actor, MediaInfo, Metadata
from .base import Source, abs_url, clean_text, dedupe, json_ld, parse_date, parse_runtime, soup_of

# 一本道系 UCNAME 中的画质标签不作为类型
_QUALITY_TAGS = {"1080p", "720p", "4k", "60fps", "hd", "fullhd", "ultra hd"}


class Caribbean(Source):
    name = "caribbean"
    label = "加勒比官方"
    kinds = frozenset({KIND_UNCENSORED})
    probe_url = "https://www.caribbeancom.com/"
    SITES = {
        "Caribbeancom": "https://www.caribbeancom.com",
        "Caribbeancompr": "https://www.caribbeancompr.com",
    }

    def supports(self, info: MediaInfo) -> bool:
        return info.kind == KIND_UNCENSORED and info.studio in self.SITES

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        base = self.SITES.get(info.studio)
        if not base or not info.number:
            return []
        url = f"{base}/moviepages/{info.number}/index.html"
        r = self.get(url)
        if r.status != 200:
            return []
        soup = soup_of(r.text)
        h1 = soup.select_one("h1[itemprop=name]") or soup.find("h1")
        title = clean_text(h1.get_text()) if h1 else ""
        if not title:
            return []
        meta = Metadata(
            source=self.name, number=info.number, studio=info.studio, label=info.studio,
            title=title, original_title=title, premiered=info.date, url=url, source_id=info.number,
            poster_url=f"{base}/moviepages/{info.number}/images/jacket.jpg",
            fanart_url=f"{base}/moviepages/{info.number}/images/l_l.jpg",
        )
        desc = soup.select_one("p[itemprop=description]")
        meta.plot = clean_text(desc.get_text()) if desc else ""
        for li in soup.select("li.movie-spec, li.movie-detail__spec"):
            key_tag = li.find(class_=re.compile(r'spec-title|spec__title'))
            key = clean_text(key_tag.get_text()) if key_tag else clean_text(li.get_text())[:6]
            links = [clean_text(a.get_text()) for a in li.find_all("a")]
            text = clean_text(li.get_text())
            if key.startswith("出演"):
                meta.actors = [Actor(name=n) for n in dedupe(links) if n]
            elif key.startswith("再生時間"):
                meta.runtime = parse_runtime(text)
            elif key.startswith("シリーズ"):
                meta.series = links[0] if links else ""
            elif key.startswith("タグ"):
                meta.genres = dedupe(links)
            elif key.startswith(("配信日", "販売日")):
                meta.premiered = parse_date(text) or meta.premiered
        return [meta]


class D2Pass(Source):
    """一本道系共用的 movie_details JSON 接口"""
    name = "1pondo"
    label = "一本道系官方"
    kinds = frozenset({KIND_UNCENSORED})
    probe_url = "https://www.1pondo.tv/"
    SITES = {
        "1Pondo": "https://www.1pondo.tv",
        "10musume": "https://www.10musume.com",
        "Pacopacomama": "https://www.pacopacomama.com",
        "Muramura": "https://www.muramura.tv",
    }

    def supports(self, info: MediaInfo) -> bool:
        return info.kind == KIND_UNCENSORED and info.studio in self.SITES

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        base = self.SITES.get(info.studio)
        if not base or not info.number:
            return []
        r = self.get(f"{base}/dyn/phpauto/movie_details/movie_id/{info.number}.json")
        if r.status != 200:
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        if not isinstance(data, dict) or not data.get("Title"):
            return []
        image = data.get("ThumbUltra") or data.get("ThumbHigh") or data.get("MovieThumb") or ""
        actors = data.get("ActressesJa") or ([data["Actor"]] if data.get("Actor") else [])
        rating = data.get("AvgRating")
        meta = Metadata(
            source=self.name,
            number=data.get("MovieID") or info.number,
            title=clean_text(data.get("Title")),
            original_title=clean_text(data.get("Title")),
            plot=clean_text(data.get("Desc")),
            studio=info.studio,
            label=info.studio,
            series=clean_text(data.get("Series")),
            premiered=parse_date(data.get("Release")) or info.date,
            runtime=max(0, int(data.get("Duration") or 0)) // 60,
            rating=round(float(rating) * 2, 1) if isinstance(rating, (int, float)) else 0.0,
            genres=[g for g in dedupe(data.get("UCNAME") or []) if g.lower() not in _QUALITY_TAGS],
            actors=[Actor(name=n) for n in dedupe(actors)],
            fanart_url=abs_url(base, image),
            url=f"{base}/movies/{info.number}/",
            source_id=info.number,
        )
        return [meta]


class Heyzo(Source):
    name = "heyzo"
    label = "HEYZO 官方"
    kinds = frozenset({KIND_UNCENSORED})
    probe_url = "https://www.heyzo.com/"

    def supports(self, info: MediaInfo) -> bool:
        return info.kind == KIND_UNCENSORED and info.studio == "HEYZO"

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        m = re.search(r'(\d{4})$', info.number or "")
        if not m:
            return []
        num = m.group(1)
        url = f"https://www.heyzo.com/moviepages/{num}/index.html"
        r = self.get(url)
        if r.status != 200:
            return []
        soup = soup_of(r.text)
        ld = next((d for d in json_ld(soup) if d.get("@type") == "Movie"), {})
        h1 = soup.find("h1")
        title = clean_text(ld.get("name")) or (clean_text(h1.get_text()).split(" - ")[0] if h1 else "")
        if not title:
            return []
        actor_ld = ld.get("actor")
        actor_list = actor_ld if isinstance(actor_ld, list) else ([actor_ld] if isinstance(actor_ld, dict) else [])
        actors = [Actor(name=clean_text(a.get("name")), thumb=abs_url(url, a.get("image"))) for a in actor_list if a.get("name")]
        meta = Metadata(
            source=self.name, number=f"HEYZO-{num}", studio="HEYZO", label="HEYZO",
            title=title, original_title=title, plot=clean_text(ld.get("description")),
            premiered=parse_date(ld.get("dateCreated") or ""), runtime=parse_runtime(ld.get("duration") or ""),
            actors=actors, fanart_url=abs_url(url, ld.get("image") or f"/contents/3000/{num}/images/player_thumbnail.jpg"),
            url=url, source_id=num,
        )
        table = soup.select_one("table.movieInfo")
        if table:
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) < 2:
                    continue
                key = clean_text(cells[0].get_text())
                if key.startswith("シリーズ"):
                    series = clean_text(cells[1].get_text())
                    meta.series = "" if set(series) <= {"-"} else series
                elif key.startswith(("タグ", "女優タイプ")):
                    meta.genres = dedupe(meta.genres + [a.get_text() for a in cells[1].find_all("a")])
                elif key.startswith("出演") and not meta.actors:
                    meta.actors = [Actor(name=n) for n in dedupe(a.get_text() for a in cells[1].find_all("a"))]
                elif key.startswith("公開日") and not meta.premiered:
                    meta.premiered = parse_date(cells[1].get_text())
        return [meta]
