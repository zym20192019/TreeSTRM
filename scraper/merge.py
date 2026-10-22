"""候选打分、多站点合并、按优先级逐站采集"""

from __future__ import annotations

import re
from datetime import date as Date
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import requests

from .http import SourceUnavailable
from .identify import numbers_match
from .models import (
    KIND_FC2, KIND_HENTAI, KIND_JAV, KIND_MGS, KIND_UNCENSORED, KIND_UNKNOWN, KIND_WESTERN,
    Actor, MediaInfo, Metadata, ScrapeOutcome,
)
from .sources import Registry
from .sources.base import is_placeholder_image
from .sources.tpdb import SITE_ALIASES

ID_KINDS = {KIND_FC2, KIND_JAV, KIND_MGS, KIND_UNCENSORED}
# 类型（genre）优先取中文站点，其余字段按站点优先级
GENRE_PREFERENCE = ("javbus", "javdb")


def _norm(value: str) -> str:
    return re.sub(r'[^0-9a-z぀-ヿ一-鿿]', '', (value or "").lower())


def _site_key(value: str) -> str:
    key = _norm(value)
    return SITE_ALIASES.get(key, key)


def _date_diff(a: str, b: str) -> Optional[int]:
    try:
        return abs((Date.fromisoformat(a[:10]) - Date.fromisoformat(b[:10])).days)
    except (TypeError, ValueError):
        return None


def text_similarity(hint: str, title: str) -> float:
    a, b = _norm(hint), _norm(title)
    if not a or not b:
        return 0.0
    if len(b) >= 6 and (b in a or a in b):
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def performer_match(hint: str, actors: List[Actor]) -> float:
    h = _norm(hint)
    if not h or not actors:
        return 0.0
    for a in actors:
        name = _norm(a.name)
        if len(name) >= 4 and name in h:
            return 1.0
        # 文件名常只有名字不含姓氏
        first = _norm((a.name or "").split(" ")[0])
        if len(first) >= 4 and first in h:
            return 0.7
    return 0.0


def score(info: MediaInfo, meta: Metadata) -> float:
    """候选与识别结果的匹配置信度 0-1"""
    if info.kind in ID_KINDS:
        if not meta.number or not numbers_match(meta.number, info.number):
            return 0.0
        s = 0.8 + (0.1 if meta.has_image else 0.0) + (0.1 if meta.title else 0.0)
        # 无码番号自带日期，站点日期差太多说明是同号不同厂牌
        if info.date and meta.premiered:
            diff = _date_diff(info.date, meta.premiered)
            if diff is not None and diff > 3:
                s -= 0.25
        return round(max(0.0, min(1.0, s)), 3)

    if info.kind == KIND_HENTAI:
        return round(text_similarity(info.title_hint or info.number, meta.title), 3)

    if info.kind == KIND_WESTERN:
        site = 0.0
        if info.site:
            want = _site_key(info.site)
            for have in (meta.studio, meta.label):
                key = _site_key(have)
                if not key:
                    continue
                if key == want:
                    site = 1.0
                    break
                if len(want) >= 4 and (want in key or key in want):
                    site = max(site, 0.6)
        when = 0.0
        if info.date and meta.premiered:
            diff = _date_diff(info.date, meta.premiered)
            when = 1.0 if diff == 0 else (0.8 if diff == 1 else 0.0)
        text = max(performer_match(info.title_hint, meta.actors), text_similarity(info.title_hint, meta.title))
        if info.site and info.date:
            s = 0.45 * site + 0.35 * when + 0.2 * text
        elif info.site:
            s = 0.45 * site + 0.55 * text
        elif info.date:
            s = 0.35 * when + 0.65 * text
        else:
            s = 0.9 * text
        # 文件名带日期而候选日期对不上：同站同演员的其他场景，只能进「待确认」
        if info.date and when == 0:
            s = min(s, 0.5)
        return round(s, 3)
    return 0.0


def _first(values) -> object:
    for v in values:
        if v:
            return v
    return None


def merge(info: MediaInfo, accepted: List[Metadata]) -> Tuple[Metadata, List[Dict[str, str]]]:
    """accepted 已按站点优先级排序；同一作品的字段逐项取第一个非空值"""
    primary = accepted[0]
    out = Metadata(source=primary.source)
    for name in ("title", "original_title", "plot", "studio", "label", "series", "director", "premiered", "url", "source_id"):
        setattr(out, name, _first(getattr(m, name) for m in accepted) or "")
    out.runtime = int(_first(m.runtime for m in accepted) or 0)
    out.rating = float(_first(m.rating for m in accepted) or 0.0)
    out.tags = list(_first(m.tags for m in accepted) or [])
    by_pref = sorted(accepted, key=lambda m: (m.source not in GENRE_PREFERENCE, GENRE_PREFERENCE.index(m.source) if m.source in GENRE_PREFERENCE else 0))
    out.genres = list(_first(m.genres for m in by_pref) or [])
    out.extrafanart = list(_first(m.extrafanart for m in accepted) or [])
    out.score = max(m.score for m in accepted)

    actors = [Actor(a.name, a.thumb, a.role) for a in (_first(m.actors for m in accepted) or [])]
    # 头像从其他站点按名字补齐
    for a in actors:
        if a.thumb:
            continue
        for m in accepted:
            hit = next((x for x in m.actors if x.thumb and _norm(x.name) == _norm(a.name)), None)
            if hit:
                a.thumb = hit.thumb
                break
    out.actors = actors

    out.number = info.number if info.kind in ID_KINDS else (primary.number or info.number)
    if info.kind in ID_KINDS and not out.premiered and info.date:
        out.premiered = info.date

    images: List[Dict[str, str]] = []
    seen = set()
    for kind in ("poster", "cover", "fanart"):
        for m in accepted:
            url = getattr(m, f"{kind}_url")
            if url and url not in seen and not is_placeholder_image(url):
                seen.add(url)
                images.append({"url": url, "type": kind, "referer": m.image_referer or m.url, "source": m.source})
    out.poster_url = next((i["url"] for i in images if i["type"] == "poster"), "")
    out.cover_url = next((i["url"] for i in images if i["type"] == "cover"), "")
    out.fanart_url = next((i["url"] for i in images if i["type"] == "fanart"), "")
    return out, images


def _complete(info: MediaInfo, merged: Metadata, images: List[Dict[str, str]]) -> bool:
    if not (merged.title and merged.premiered and images):
        return False
    if info.kind in (KIND_JAV, KIND_MGS, KIND_UNCENSORED) and not merged.actors:
        return False
    # FC2 官方图只有 320x200，还要从其他站点拿一张大图
    if info.kind == KIND_FC2 and not any(i["source"] != "fc2" for i in images):
        return False
    return True


def collect(registry: Registry, info: MediaInfo, *, min_score: float = 0.6, max_sources: int = 3,
            only: Optional[List[str]] = None) -> ScrapeOutcome:
    """按优先级逐站查询直到资料完整；只有达到 min_score 的候选参与合并"""
    if info.kind == KIND_UNKNOWN:
        return ScrapeOutcome("not_found", info, message="无法从文件名识别番号或站点")
    chain = registry.chain(info)
    if only:
        chain = [s for s in chain if s.name in only]
    if not chain:
        return ScrapeOutcome("error", info, message="没有可用于该类型的已启用站点")

    candidates: List[Metadata] = []
    accepted: List[Metadata] = []
    errors: List[str] = []
    queried = 0
    hit_sources = 0
    for src in chain:
        if hit_sources >= max(1, max_sources):
            break
        try:
            found = src.fetch(info)
        except SourceUnavailable:
            errors.append(f"{src.label}: 熔断冷却中")
            continue
        except requests.RequestException as exc:
            errors.append(f"{src.label}: {type(exc).__name__}")
            continue
        except Exception as exc:  # 单个站点解析异常不影响其他站点
            errors.append(f"{src.label}: 解析失败 {type(exc).__name__}: {exc}"[:200])
            continue
        queried += 1
        good = []
        for m in found:
            m.score = score(info, m)
            candidates.append(m)
            if m.score >= min_score:
                good.append(m)
        if not good:
            continue
        hit_sources += 1
        if info.kind in ID_KINDS:
            accepted.append(max(good, key=lambda m: m.score))
        else:
            # 欧美 / 里番是按内容检索，多个候选不一定是同一作品，只取最高分的那个
            best = max(good, key=lambda m: m.score)
            if not accepted or best.score > accepted[0].score:
                accepted = [best]
            if best.score >= 0.85:
                break
        merged, images = merge(info, accepted)
        if _complete(info, merged, images):
            break

    candidates.sort(key=lambda m: m.score, reverse=True)
    message = "；".join(errors)
    if accepted:
        merged, images = merge(info, accepted)
        if merged.title:
            return ScrapeOutcome("matched", info, merged, candidates[:15], images, [m.source for m in accepted], message)
    if candidates:
        return ScrapeOutcome("low_confidence", info, None, candidates[:15], message=message or "候选置信度不足，请人工确认")
    if queried == 0 and errors:
        return ScrapeOutcome("error", info, message=message)
    return ScrapeOutcome("not_found", info, message=message or "所有站点均未找到")


def from_candidate(registry: Registry, info: MediaInfo, candidate: Metadata, *, supplement: bool = True,
                   max_sources: int = 3) -> ScrapeOutcome:
    """人工选定候选：必要时按站点 ID 重取完整详情，ID 类再用番号从其他站点补齐字段"""
    src = registry.sources.get(candidate.source)
    meta = candidate
    if src and candidate.source_id:
        try:
            full = src.fetch_by_id(candidate.source_id)
        except (SourceUnavailable, requests.RequestException):
            full = None
        if full:
            meta = full
    meta.score = 1.0
    accepted = [meta]
    target = info
    if meta.number and info.kind in ID_KINDS | {KIND_UNKNOWN}:
        from .identify import info_from_override
        target = info_from_override(meta.number, info)
    if supplement and target.kind in ID_KINDS:
        extra = collect(registry, target, max_sources=max_sources)
        if extra.meta and numbers_match(extra.meta.number, target.number):
            used = {meta.source}
            for c in extra.candidates:
                if c.score >= 0.6 and c.source not in used:
                    used.add(c.source)
                    accepted.append(c)
    merged, images = merge(target, accepted)
    merged.score = 1.0
    return ScrapeOutcome("matched", target, merged, accepted, images, [m.source for m in accepted], "人工指定")
