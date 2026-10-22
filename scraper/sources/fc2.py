"""FC2 系：官方站、FC2PPVDB、FC2Club、海外镜像、搜索引擎快照（仅补标题）"""

from __future__ import annotations

import re
from typing import List

from ..models import KIND_FC2, Actor, MediaInfo, Metadata
from .base import Source, abs_url, clean_text, dedupe, og, parse_date, parse_runtime, soup_of

FC2_HEADERS = {"Cookie": "age_check=1; adult=1; contents_adult=1; wei6H=1", "Accept-Language": "ja,zh-CN;q=0.8"}
# 正常页面里也有 data-message="notfound" 等片段，只认下架页专属的容器 / 图标
_NOT_FOUND_MARKS = ("items_notfound_wp", "notfound/icon_404bg", "お探しの商品が見つかりませんでした", "找不到您要的商品")


def fc2_id(info: MediaInfo) -> str:
    m = re.search(r'(\d{5,8})$', info.number or "")
    return m.group(1) if m else ""


def _fc2_meta(fid: str, source: str, **kwargs) -> Metadata:
    meta = Metadata(source=source, number=f"FC2-PPV-{fid}", studio="FC2-PPV", **kwargs)
    if "FC2" not in meta.tags:
        meta.tags = ["FC2"] + meta.tags
    return meta


class FC2Official(Source):
    name = "fc2"
    label = "FC2 官方"
    kinds = frozenset({KIND_FC2})
    probe_url = "https://adult.contents.fc2.com/"

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        fid = fc2_id(info)
        if not fid:
            return []
        url = f"https://adult.contents.fc2.com/article/{fid}/"
        r = self.get(url, headers=FC2_HEADERS)
        if r.status != 200 or any(mark in r.text for mark in _NOT_FOUND_MARKS):
            return []
        soup = soup_of(r.text)

        # 标题里混有 zoom:0.01 的隐藏反爬字符串，优先用 og:title；退回 h3 时剔除隐藏元素
        title = og(soup, "title")
        if not title:
            h3 = soup.select_one('[data-section="userInfo"] h3')
            if h3:
                for hidden in h3.find_all(style=re.compile(r'zoom\s*:\s*0\.0|display\s*:\s*none')):
                    hidden.decompose()
                title = clean_text(h3.get_text())
        title = re.sub(rf'^FC2[-_ ]?PPV[-_ ]?{fid}\s*', '', title).strip()
        if not title:
            return []

        seller = soup.select_one(".items_article_writer a, a[data-article-seller-name]")
        date = ""
        for p in soup.select(".items_article_softDevice p"):
            text = p.get_text()
            if re.search(r'販売日|上架|贩卖日|Release', text):
                date = parse_date(text)
        plot = re.sub(rf'^FC2[-_ ]?PPV[-_ ]?{fid}\s*', '', og(soup, "description")).strip()
        image = og(soup, "image")
        if not image:
            thumb = soup.select_one(".items_article_MainitemThumb img")
            image = abs_url(url, thumb.get("src")) if thumb else ""
        runtime_tag = soup.select_one(".items_article_info")

        meta = _fc2_meta(
            fid, self.name,
            title=title,
            original_title=title,
            plot=plot,
            label=clean_text(seller.get_text()) if seller else "",
            premiered=date,
            runtime=parse_runtime(runtime_tag.get_text()) if runtime_tag else 0,
            tags=dedupe(a.get_text() for a in soup.select(".items_article_TagArea a")),
            # FC2 商品图是普通横图，不能按日式封套裁剪
            fanart_url=abs_url(url, image),
            poster_url=abs_url(url, image),
            extrafanart=dedupe(abs_url(url, a.get("href")) for a in soup.select(".items_article_SampleImagesArea a"))[:20],
            url=url,
            source_id=fid,
        )
        if meta.label:
            meta.director = meta.label
        return [meta]


class FC2PPVDB(Source):
    name = "fc2ppvdb"
    label = "FC2PPVDB"
    kinds = frozenset({KIND_FC2})
    probe_url = "https://fc2ppvdb.com/"

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        fid = fc2_id(info)
        if not fid:
            return []
        url = f"https://fc2ppvdb.com/articles/{fid}"
        r = self.get(url)
        if r.status != 200 or fid not in r.text:
            return []
        soup = soup_of(r.text)
        heading = soup.find("h2") or soup.find("h1")
        title = clean_text(heading.get_text()) if heading else ""
        title = re.sub(rf'^(?:FC2[-_ ]?PPV[-_ ]?)?{fid}\s*', '', title).strip()
        img = None
        for candidate in soup.find_all("img"):
            src = candidate.get("src") or candidate.get("data-src") or ""
            if fid in src or "storage" in src:
                img = src
                break
        text = soup.get_text(" ")
        m = re.search(r'(?:販売日|贩卖日|販売開始日)\s*[:：]?\s*([0-9/\-]{8,10})', text)
        meta = _fc2_meta(
            fid, self.name,
            title=title,
            original_title=title,
            premiered=parse_date(m.group(1)) if m else "",
            actors=[Actor(name=n) for n in dedupe(a.get_text() for a in soup.select('a[href*="/actresses/"]'))],
            tags=dedupe(a.get_text() for a in soup.select('a[href*="/tags/"]')),
            poster_url=abs_url(url, img) if img else "",
            url=url,
            source_id=fid,
        )
        seller = soup.select_one('a[href*="/writers/"]')
        if seller:
            meta.label = clean_text(seller.get_text())
        return [meta] if (meta.title or meta.poster_url) else []


class FC2Club(Source):
    name = "fc2club"
    label = "FC2Club"
    kinds = frozenset({KIND_FC2})
    probe_url = "https://fc2club.top/"
    note = "域名已停放时会自动熔断"

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        fid = fc2_id(info)
        if not fid:
            return []
        url = f"https://fc2club.top/html/FC2-{fid}.html"
        r = self.get(url)
        if r.status != 200 or "show-top-grids" not in r.text:
            return []
        top = soup_of(r.text).find("div", class_="show-top-grids")
        if not top:
            return []
        title = clean_text(top.find("h3").get_text()) if top.find("h3") else ""
        img = top.find("img")
        return [_fc2_meta(
            fid, self.name,
            title=re.sub(rf'^FC2[-_ ]?(?:PPV[-_ ]?)?{fid}\s*', '', title).strip(),
            poster_url=abs_url("https://fc2club.top/", img.get("src")) if img else "",
            url=url, source_id=fid,
        )]


class FC2Mirror(Source):
    name = "fc2mirror"
    label = "FC2 镜像站"
    kinds = frozenset({KIND_FC2})
    probe_url = "https://fc2jav.com/"
    default_enabled = False
    note = "海外镜像，图片匹配较宽松，默认关闭"

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        fid = fc2_id(info)
        if not fid:
            return []
        for url in (f"https://fc2jav.com/fc2-ppv-{fid}/", f"https://maxjav.com/{fid}/"):
            try:
                r = self.get(url)
            except Exception:
                continue
            if r.status != 200:
                continue
            soup = soup_of(r.text)
            h1 = soup.find("h1")
            title = clean_text(h1.get_text()) if h1 else ""
            # 严格校验：标题必须含编号，图片地址必须含编号，宁缺毋滥
            if fid not in title:
                continue
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
                if fid in src:
                    return [_fc2_meta(
                        fid, self.name,
                        title=re.sub(rf'^.*?{fid}\s*', '', title).strip(),
                        poster_url=abs_url(url, src),
                        url=url, source_id=fid,
                    )]
        return []


class FC2Snapshot(Source):
    """搜索引擎快照：只为官方已下架的作品补日文原标题，不提供图片"""
    name = "fc2snapshot"
    label = "搜索引擎快照"
    kinds = frozenset({KIND_FC2})
    probe_url = "https://html.duckduckgo.com/html/"

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        fid = fc2_id(info)
        if not fid:
            return []
        r = self.get("https://html.duckduckgo.com/html/", params={"q": f"FC2-PPV-{fid}"})
        if r.status not in (200, 202):
            return []
        for res in soup_of(r.text).find_all("div", class_="result"):
            parts = [res.find("a", class_="result__title"), res.find("a", class_="result__snippet")]
            text = " ".join(clean_text(p.get_text()) for p in parts if p)
            if fid not in text or not re.search(r'[぀-ヿ]', text):
                continue
            m = re.search(rf'(?:FC2[\s\-_]*PPV[\s\-_]*{fid}|FC2[\s\-_]*{fid})[\s:：【]*(.+?)(?:\.\.\.|…|\||$)', text)
            if not m:
                continue
            title = re.sub(r'[\-_|｜](?:在线|高清|播放|无码|FC2).*$', '', m.group(1)).strip()
            if len(title) > 8:
                return [_fc2_meta(fid, self.name, title=title, original_title=title, url=r.url, source_id=fid)]
        return []
