"""刮削结果落盘：海报裁剪、fanart/thumb、完整 NFO（保留探针写入的流信息）、原子写入"""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import time
from typing import Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from .models import KIND_FC2, KIND_LABELS, KIND_WESTERN, MediaInfo, Metadata, ScrapeOutcome

try:  # Pillow 可选：没有时退回 ffmpeg 裁剪，再不行直接用原图
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None

SCRAPER_MARK = "TreeSTRM-Scraper"
# 日式封套：右侧正面宽度约为高度的 0.7 倍
POSTER_RATIO = 0.7

OVERWRITE_MISSING = "missing"
OVERWRITE_REPLACE_AI = "replace_ai"
OVERWRITE_ALL = "all"
OVERWRITE_POLICIES = (OVERWRITE_MISSING, OVERWRITE_REPLACE_AI, OVERWRITE_ALL)

NFO_NONE, NFO_OURS, NFO_AI, NFO_OTHER = "none", "scraper", "ai", "other"


# ---------- 路径 ----------
def artifact_paths(strm_path: str) -> Dict[str, str]:
    base = strm_path[:-5] if strm_path.lower().endswith(".strm") else os.path.splitext(strm_path)[0]
    return {
        "nfo": base + ".nfo",
        "poster": base + "-poster.jpg",
        "fanart": base + "-fanart.jpg",
        "thumb": base + "-thumb.jpg",
    }


def _exists(path: str, min_bytes: int = 1000) -> bool:
    try:
        return os.path.getsize(path) >= min_bytes
    except OSError:
        return False


def _atomic_write(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---------- 图片 ----------
def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """不依赖 Pillow 读取 JPEG/PNG/GIF/WEBP 尺寸"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:4] == b"GIF8":
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
            return None
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg_len
    except (struct.error, IndexError):
        return None
    return None


def _ffmpeg_crop(data: bytes, crop_w: int, h: int, x: int) -> Optional[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-vf", f"crop={crop_w}:{h}:{x}:0", "-frames:v", "1", "-q:v", "2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            input=data, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout
    return out if proc.returncode == 0 and out[:3] == b"\xff\xd8\xff" else None


def crop_cover(data: bytes) -> Optional[bytes]:
    """日式横版封套裁出右侧正面作为竖版海报；不是横图时返回 None"""
    size = image_size(data)
    if not size:
        return None
    w, h = size
    if h <= 0 or w / h <= 1.2:
        return None
    crop_w = min(w, int(round(h * POSTER_RATIO)))
    x = w - crop_w
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as img:
                part = img.crop((x, 0, w, h)).convert("RGB")
                buf = io.BytesIO()
                part.save(buf, "JPEG", quality=92)
                return buf.getvalue()
        except Exception:
            pass
    return _ffmpeg_crop(data, crop_w, h, x)


def to_jpeg(data: bytes) -> bytes:
    if data[:3] == b"\xff\xd8\xff" or Image is None:
        return data
    try:
        with Image.open(io.BytesIO(data)) as img:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=92)
            return buf.getvalue()
    except Exception:
        return data


class ImagePicker:
    """按优先级下载图片，结果在一次写入内复用"""

    def __init__(self, images: List[Dict[str, str]], download: Callable[[str, str], Optional[bytes]], kind: str):
        self.images = images
        self.download = download
        self.kind = kind
        self._cache: Dict[str, Optional[bytes]] = {}

    def _get(self, img: Dict[str, str]) -> Optional[bytes]:
        url = img["url"]
        if url not in self._cache:
            self._cache[url] = self.download(url, img.get("referer", ""))
        return self._cache[url]

    def _first(self, types: Tuple[str, ...], largest_of: int = 1) -> Tuple[Optional[bytes], str]:
        best, best_type, best_area, tried = None, "", -1, 0
        for img in self.images:
            if img["type"] not in types:
                continue
            data = self._get(img)
            if not data:
                continue
            size = image_size(data) or (0, 0)
            area = size[0] * size[1] or len(data) // 10
            if area > best_area:
                best, best_type, best_area = data, img["type"], area
            tried += 1
            if tried >= largest_of:
                break
        return best, best_type

    def poster(self, crop: bool) -> Optional[bytes]:
        # FC2 各站图片质量差别很大（官方仅 320x200），取前三张里最大的
        largest = 3 if self.kind == KIND_FC2 else 1
        data, _ = self._first(("poster",), largest)
        if data:
            return to_jpeg(data)
        data, _ = self._first(("cover",))
        if data:
            cropped = crop_cover(data) if crop else None
            return cropped or to_jpeg(data)
        data, _ = self._first(("fanart",), largest)
        return to_jpeg(data) if data else None

    def fanart(self) -> Optional[bytes]:
        largest = 3 if self.kind == KIND_FC2 else 1
        for types in (("fanart",), ("cover",), ("poster",)):
            data, _ = self._first(types, largest)
            if data:
                return to_jpeg(data)
        return None


# ---------- NFO ----------
def classify_nfo(path: str) -> str:
    """none：不存在；scraper：本模块写的；ai：治理流水线 AI 生成；other：其他刮削器 / 人工"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            text = f.read(65536)
    except OSError:
        return NFO_NONE
    lower = text.lower()
    if "<movie" not in lower or "<title" not in lower:
        return NFO_NONE
    if "<scraper>" in lower:
        return NFO_OURS
    if any(t in lower for t in ("<premiered>", "<actor>", "<uniqueid", "<num>", "<releasedate>")):
        return NFO_OTHER
    return NFO_AI


def _preserved_nodes(path: str) -> Tuple[Optional[ET.Element], str, str]:
    """保留旧 NFO 中探针写入的 fileinfo / runtime / filesize"""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None, "", ""
    fileinfo = root.find("fileinfo")
    has_probe = fileinfo is not None and fileinfo.find("streamdetails/video/durationinseconds") is not None
    runtime = (root.findtext("runtime") or "").strip() if has_probe else ""
    filesize = (root.findtext("filesize") or "").strip()
    return fileinfo, runtime, filesize


def display_title(meta: Metadata, info: MediaInfo, with_number: bool) -> str:
    title = meta.title or meta.original_title or info.raw
    if with_number and meta.number and info.kind != KIND_WESTERN and meta.number.lower() not in title.lower():
        title = f"{meta.number} {title}"
    return title


def build_nfo(meta: Metadata, info: MediaInfo, *, title_with_number: bool, old_path: str = "") -> bytes:
    root = ET.Element("movie")

    def add(tag: str, text, parent: ET.Element = root, **attrs) -> Optional[ET.Element]:
        if text in (None, "", 0, 0.0):
            return None
        el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
        el.text = str(text)
        return el

    title = display_title(meta, info, title_with_number)
    add("title", title)
    add("originaltitle", meta.original_title or meta.title)
    add("sorttitle", meta.number or title)
    add("plot", meta.plot)
    add("outline", meta.plot)
    if meta.premiered:
        add("premiered", meta.premiered)
        add("releasedate", meta.premiered)
        add("year", meta.premiered[:4])

    fileinfo, old_runtime, old_filesize = _preserved_nodes(old_path) if old_path else (None, "", "")
    add("runtime", old_runtime or (meta.runtime or ""))
    add("studio", meta.studio or meta.label)
    add("label", meta.label)
    add("publisher", meta.label or meta.studio)
    add("director", meta.director)
    if meta.series:
        s = ET.SubElement(root, "set")
        add("name", meta.series, s)
    if meta.rating:
        add("rating", f"{meta.rating:.1f}")

    genres = list(meta.genres)
    tags = list(meta.tags)
    if info.subtitle:
        genres.append("中文字幕")
    if info.uncensored_leak:
        tags.append("无码流出")
    tags.append(KIND_LABELS.get(info.kind, ""))
    seen = set()
    for g in genres:
        if g and g.lower() not in seen:
            seen.add(g.lower())
            add("genre", g)
    seen_tags = set()
    for t in tags + ([meta.series] if meta.series else []):
        if t and t.lower() not in seen_tags:
            seen_tags.add(t.lower())
            add("tag", t)

    for order, actor in enumerate(meta.actors):
        a = ET.SubElement(root, "actor")
        add("name", actor.name, a)
        add("type", "Actor", a)
        add("role", actor.role, a)
        add("order", str(order), a)
        add("thumb", actor.thumb, a)

    if meta.number:
        add("num", meta.number)
        add("uniqueid", meta.number, type="num", default="true")
    if meta.source_id and meta.source:
        add("uniqueid", meta.source_id, type=meta.source)
    add("website", meta.url)
    poster_remote = meta.poster_url or meta.cover_url
    add("thumb", poster_remote, aspect="poster")
    fanart_urls = [u for u in [meta.fanart_url or meta.cover_url] + meta.extrafanart if u]
    if fanart_urls:
        fan = ET.SubElement(root, "fanart")
        for u in fanart_urls[:20]:
            add("thumb", u, fan)
    add("scraper", SCRAPER_MARK)
    add("scrapersource", meta.source)
    add("dateadded", time.strftime("%Y-%m-%d %H:%M:%S"))
    if old_filesize:
        add("filesize", old_filesize)
    if fileinfo is not None:
        root.append(fileinfo)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ---------- 写入 ----------
def plan(strm_path: str, policy: str, write_fanart: bool = True) -> Dict[str, bool]:
    """按覆盖策略决定哪些产物需要写；全部 False 表示无需处理"""
    p = artifact_paths(strm_path)
    # 「只补缺失」时只看文件是否存在，十万级条目扫描不必逐个读取 NFO
    if policy == OVERWRITE_MISSING:
        nfo_kind = NFO_OTHER if _exists(p["nfo"], 50) else NFO_NONE
    else:
        nfo_kind = classify_nfo(p["nfo"])
    force = policy == OVERWRITE_ALL
    replace_ai = policy == OVERWRITE_REPLACE_AI and nfo_kind == NFO_AI
    return {
        "nfo": force or replace_ai or nfo_kind == NFO_NONE,
        # AI NFO 对应的海报是 CD2 抽帧，替换 AI NFO 时一并换成官方海报
        "poster": force or replace_ai or not _exists(p["poster"]),
        "fanart": write_fanart and (force or replace_ai or not _exists(p["fanart"])),
        "thumb": write_fanart and (force or replace_ai or not _exists(p["thumb"])),
    }


def write_outcome(strm_path: str, outcome: ScrapeOutcome, *, policy: str, download: Callable[[str, str], Optional[bytes]],
                  title_with_number: bool = False, crop_poster: bool = True, write_fanart: bool = True,
                  todo: Optional[Dict[str, bool]] = None) -> Dict[str, bool]:
    """返回实际写入了哪些产物"""
    meta = outcome.meta
    written = {"nfo": False, "poster": False, "fanart": False, "thumb": False}
    if meta is None:
        return written
    p = artifact_paths(strm_path)
    todo = todo or plan(strm_path, policy, write_fanart)
    picker = ImagePicker(outcome.images, download, outcome.info.kind)

    if todo["poster"]:
        data = picker.poster(crop_poster)
        if data:
            _atomic_write(p["poster"], data)
            written["poster"] = True
    if write_fanart and (todo["fanart"] or todo["thumb"]):
        data = picker.fanart()
        if data:
            if todo["fanart"]:
                _atomic_write(p["fanart"], data)
                written["fanart"] = True
            if todo["thumb"]:
                _atomic_write(p["thumb"], data)
                written["thumb"] = True
    if todo["nfo"]:
        old = p["nfo"] if os.path.exists(p["nfo"]) else ""
        _atomic_write(p["nfo"], build_nfo(meta, outcome.info, title_with_number=title_with_number, old_path=old))
        written["nfo"] = True
    return written
