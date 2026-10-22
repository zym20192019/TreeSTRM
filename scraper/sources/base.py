"""站点解析器基类与通用解析工具"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import Fetcher, Response
from ..models import Actor, MediaInfo, Metadata


class Context:
    """解析器运行环境：网络层 + 读取当前配置的函数"""

    def __init__(self, fetcher: Fetcher, settings: Callable[[], Dict[str, Any]]):
        self.fetcher = fetcher
        self.settings = settings


class Source:
    name = ""
    label = ""
    kinds: frozenset = frozenset()
    default_enabled = True
    # 只需验证访问性的首页 / 接口，供“站点健康检查”使用
    probe_url = ""
    note = ""

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def supports(self, info: MediaInfo) -> bool:
        return info.kind in self.kinds

    def fetch(self, info: MediaInfo) -> List[Metadata]:
        """按识别结果抓取，返回候选列表（ID 类站点通常 0 或 1 个）"""
        raise NotImplementedError

    def search(self, query: str, info: MediaInfo) -> List[Metadata]:
        """手动搜索：默认把关键词当作番号重新识别后抓取"""
        from ..identify import info_from_override
        return self.fetch(info_from_override(query, info))

    def fetch_by_id(self, source_id: str) -> Optional[Metadata]:
        """手动选择候选后按站点 ID 精确重取（列表页结果字段不全时使用）"""
        return None

    # ---------- 工具 ----------
    def get(self, url: str, **kwargs) -> Response:
        return self.ctx.fetcher.get(self.name, url, **kwargs)


def soup_of(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "html.parser")


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r'\s+', ' ', value).strip()


def abs_url(base: str, url: Optional[str]) -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base, url)


def parse_date(value: Optional[str]) -> str:
    """把 2021/02/18、2021-2-8、2021年2月18日 统一成 YYYY-MM-DD"""
    if not value:
        return ""
    m = re.search(r'((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})', value)
    if not m:
        return ""
    y, mo, d = m.groups()
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return ""
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def parse_runtime(value: Any) -> int:
    """'150分鐘' / '120min' / '01:03:39' / 'PT1H3M39S' / 秒数 → 分钟"""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    m = re.fullmatch(r'\s*PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?\s*', s)
    if m and any(m.groups()):
        h, mi, sec = (int(x or 0) for x in m.groups())
        return h * 60 + mi + (1 if sec >= 30 else 0)
    m = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', s)
    if m:
        h, mi, sec = (int(x) for x in m.groups())
        return h * 60 + mi + (1 if sec >= 30 else 0)
    m = re.search(r'(\d{1,3}):(\d{2})(?!\d)', s)
    if m:
        return int(m.group(1)) + (1 if int(m.group(2)) >= 30 else 0)
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else 0


def dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for v in values:
        v = clean_text(v)
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def actors_from_names(names: Iterable[str]) -> List[Actor]:
    return [Actor(name=n) for n in dedupe(names)]


def og(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": f"og:{prop}"}) or soup.find("meta", attrs={"name": f"og:{prop}"})
    return clean_text(tag.get("content")) if tag else ""


def json_ld(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            out.append(data)
    return out


def strip_number_prefix(title: str, number: str) -> str:
    """站点标题常以番号开头，NFO 标题由输出层统一决定是否加番号"""
    if not title or not number:
        return clean_text(title)
    pattern = re.escape(number).replace(r'\-', r'[-_ ]?')
    return clean_text(re.sub(rf'^\s*{pattern}\s*', '', title, flags=re.IGNORECASE)) or clean_text(title)


def is_placeholder_image(url: str) -> bool:
    return bool(re.search(r'(?i)now_?printing|noimage|no_image|placeholder|dummy', url or ""))
