#!/usr/bin/env python3
"""
TreeSTRM NFO 构建器：AI 治理流水线生成的 Emby / Kodi Movie NFO
文本字段统一 XML 转义，标题/简介用 CDATA 包裹并处理 ]]> 序列
"""

from typing import Any
from xml.sax.saxutils import escape as xml_escape


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
    title = _cdata_text(meta.get("title", ""))
    summary = _cdata_text(meta.get("summary", ""))
    
    filesize_tag = f"  <filesize>{filesize}</filesize>\n" if filesize > 0 else ""
    runtime_tag = f"  <runtime>{runtime_mins}</runtime>\n" if runtime_mins > 0 else ""
    
    content = f"""<?xml version="1.0" encoding="UTF-8" ?>
<movie>
  <title><![CDATA[{title}]]></title>
  <originaltitle><![CDATA[{title}]]></originaltitle>
  <sorttitle><![CDATA[{title}]]></sorttitle>
  <studio>{studio}</studio>
  <plot><![CDATA[{summary}]]></plot>
  <outline><![CDATA[{summary}]]></outline>
{genres_xml}
{tags_xml}
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
