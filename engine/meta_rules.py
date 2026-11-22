#!/usr/bin/env python3
"""
AI 元数据后处理规则：
- AI 常把系列视频的序号一并清洗掉（多个文件标题完全相同），这里把文件名尾部的 (n) 序号补回，同目录仍撞名则退回文件名
- 「未知/未知工作室」之类占位制作公司一律丢弃，不写进 NFO
"""

import re
from typing import Iterable, List, Optional, Set

PLACEHOLDER_STUDIOS = {"", "未知", "未知工作室", "未知制作", "未知厂商", "未知作者", "unknown", "n/a", "none", "null"}
_SEQ_SUFFIX = re.compile(r"^(.*?)\s*\((\d+)\)$")
_RES_TOKEN = re.compile(r"\b\d{3,4}[pP]\b|\b[48][kK]\b")


def _numbers(text: str) -> Set[int]:
    return {int(x) for x in re.findall(r"\d+", _RES_TOKEN.sub("", text or ""))}


def clean_studio(value: object) -> str:
    """丢弃占位制作公司；返回空串表示不写 <studio>。"""
    s = str(value or "").strip()
    return "" if s.lower() in PLACEHOLDER_STUDIOS else s


def clean_people(values: object) -> List[str]:
    """AI 返回的作者/出镜者列表：去空、去占位、去重，最多 5 个。"""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: List[str] = []
    for v in values:
        name = clean_studio(v)
        if name and name not in out:
            out.append(name)
    return out[:5]


def keep_sequence(title: str, base_name: str) -> str:
    """文件名以 (n) 结尾而标题丢了这个序号时，把 (n) 补回标题。"""
    title = (title or "").strip()
    m = _SEQ_SUFFIX.match(base_name or "")
    if not title or not m:
        return title or (base_name or "")
    n = int(m.group(2))
    if n in _numbers(title) and not _numbers(m.group(1)) >= _numbers(title):
        return title
    if re.search(rf"\(\s*{n}\s*\)$", title):
        return title
    return f"{title} ({n})"


def unique_title(title: str, base_name: str, used: Set[str]) -> str:
    """同一目录内标题去重：先补序号，仍重复就退回文件名（文件名在目录内天然唯一）。"""
    t = keep_sequence(title, base_name)
    if t in used:
        t = base_name
    used.add(t)
    return t


def existing_titles(nfo_titles: Iterable[Optional[str]]) -> Set[str]:
    return {t.strip() for t in nfo_titles if t and t.strip()}
