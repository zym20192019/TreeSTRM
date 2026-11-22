#!/usr/bin/env python3
"""
TreeSTRM NFO 构建器：AI 治理流水线生成的 Emby / Kodi Movie NFO
文本字段统一 XML 转义，标题/简介用 CDATA 包裹并处理 ]]> 序列
"""

import os
import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from engine.ai_prompts import META_MARK_TAG, META_VERSION


def _xml_text(value: Any) -> str:
    """AI 返回内容可能含 & < >，不转义会生成 Emby 无法解析的坏 NFO"""
    return xml_escape(str(value or ""))

def _cdata_text(value: Any) -> str:
    # CDATA 内唯一非法序列是 ]]>，拆成两个相邻 CDATA 段
    return str(value or "").replace("]]>", "]]]]><![CDATA[>")

def write_nfo_file(nfo_path: str, meta: dict, runtime_mins: int = 0, filesize: int = 0, width: int = 1920, height: int = 1080):
    tags_xml = "\n".join([f"  <tag>{_xml_text(t)}</tag>" for t in meta.get("tags", [])])
    genres_xml = "\n".join([f"  <genre>{_xml_text(g)}</genre>" for g in meta.get("genres", [])])
    studio = _xml_text(meta.get("studio", ""))
    studio_xml = f"  <studio>{studio}</studio>\n" if studio.strip() else ""
    actors_xml = "".join(
        f"  <actor>\n    <name>{_xml_text(a)}</name>\n    <type>Actor</type>\n  </actor>\n" for a in meta.get("actors", []) if str(a).strip()
    )
    title = _cdata_text(meta.get("title", ""))
    summary = _cdata_text(meta.get("summary", ""))
    
    filesize_tag = f"  <filesize>{filesize}</filesize>\n" if filesize > 0 else ""
    runtime_tag = f"  <runtime>{runtime_mins}</runtime>\n" if runtime_mins > 0 else ""
    
    content = f"""<?xml version="1.0" encoding="UTF-8" ?>
<movie>
  <title><![CDATA[{title}]]></title>
  <originaltitle><![CDATA[{title}]]></originaltitle>
  <sorttitle><![CDATA[{title}]]></sorttitle>
{studio_xml}  <plot><![CDATA[{summary}]]></plot>
  <outline><![CDATA[{summary}]]></outline>
{genres_xml}
{tags_xml}
{actors_xml}  <{META_MARK_TAG}>{META_VERSION}</{META_MARK_TAG}>
{runtime_tag}{filesize_tag}  <fileinfo>
    <streamdetails>
      <video>
        <width>{width}</width>
        <height>{height}</height>
      </video>
    </streamdetails>
  </fileinfo>
</movie>"""
    with open(nfo_path, "w", encoding="utf-8") as f:
        f.write(content)


# update_nfo_meta 会整体替换的节点；其余（fileinfo/runtime/filesize/封面等）原样保留
_META_TAGS = ("title", "originaltitle", "sorttitle", "studio", "plot", "outline", "genre", "tag", "actor", META_MARK_TAG)


def update_nfo_meta(nfo_path: str, meta: dict) -> bool:
    """用新 meta 原地重写已有 NFO 的元数据节点，保留音视频流等其余节点；失败返回 False。"""
    try:
        tree = ET.parse(nfo_path)
    except (ET.ParseError, OSError):
        return False
    root = tree.getroot()
    keep = [el for el in list(root) if el.tag not in _META_TAGS]
    for el in list(root):
        root.remove(el)

    def add(tag: str, text: str) -> ET.Element:
        el = ET.SubElement(root, tag)
        el.text = text
        return el

    title = str(meta.get("title") or "")
    for tag in ("title", "originaltitle", "sorttitle"):
        add(tag, title)
    if str(meta.get("studio") or "").strip():
        add("studio", str(meta["studio"]).strip())
    summary = str(meta.get("summary") or "")
    if summary:
        add("plot", summary)
        add("outline", summary)
    for g in meta.get("genres") or []:
        add("genre", str(g))
    for t in meta.get("tags") or []:
        add("tag", str(t))
    for name in meta.get("actors") or []:
        a = ET.SubElement(root, "actor")
        ET.SubElement(a, "name").text = str(name)
        ET.SubElement(a, "type").text = "Actor"
    add(META_MARK_TAG, META_VERSION)
    root.extend(keep)
    ET.indent(tree, space="  ")
    # 短临时名：原名接近 255 字节上限时追加 .tmp 会超长
    tmp_path = os.path.join(os.path.dirname(nfo_path), f".nforegen-{os.getpid()}-{id(tree)}.tmp")
    try:
        tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
        os.replace(tmp_path, nfo_path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False
    return True
