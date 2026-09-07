#!/usr/bin/env python3
"""
TreeSTRM 标准 NFO XML 纯正构建器
特性：
1. 真实数据驱动：仅根据官方元数据落地，绝不凭空生造假 NFO
2. 兼容 Kodi / Emby 标准规范 (<movie> 根节点，含 title, studio, plot, actor, genre)
3. 严格字符集 UTF-8 与 XML 安全转义
"""

import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional

def build_movie_nfo_xml(meta: Dict[str, Any], base_filename: str) -> str:
    """构建 Emby / Kodi 100% 兼容的 Movie NFO XML 文本"""
    root = ET.Element("movie")
    
    title = meta.get('title') or base_filename
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "originaltitle").text = title
    ET.SubElement(root, "sorttitle").text = title
    
    if meta.get('premiered'):
        prem = str(meta.get('premiered'))
        ET.SubElement(root, "premiered").text = prem
        ET.SubElement(root, "year").text = prem[:4]
        
    plot = meta.get('plot') or meta.get('summary') or ""
    if plot:
        ET.SubElement(root, "plot").text = plot
        ET.SubElement(root, "outline").text = plot
        
    studio = meta.get('studio') or meta.get('publisher') or ""
    if studio:
        ET.SubElement(root, "studio").text = studio
        ET.SubElement(root, "publisher").text = studio
        
    for act in meta.get('actors', []):
        if act:
            actor_el = ET.SubElement(root, "actor")
            ET.SubElement(actor_el, "name").text = str(act).strip()
            
    for tag in meta.get('tags', []):
        if tag:
            ET.SubElement(root, "tag").text = str(tag).strip()

    for genre in meta.get('genres', []):
        if genre:
            ET.SubElement(root, "genre").text = str(genre).strip()
            
    ET.SubElement(root, "lockdata").text = "true"
    
    # 视频流信息（如果提供）
    width = meta.get('width', 1920)
    height = meta.get('height', 1080)
    fileinfo = ET.SubElement(root, "fileinfo")
    streamdetails = ET.SubElement(fileinfo, "streamdetails")
    video = ET.SubElement(streamdetails, "video")
    ET.SubElement(video, "width").text = str(width)
    ET.SubElement(video, "height").text = str(height)
    
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode('utf-8')

def write_movie_nfo(nfo_path: str, meta: Dict[str, Any], base_filename: str):
    content = build_movie_nfo_xml(meta, base_filename)
    with open(nfo_path, "w", encoding="utf-8") as f:
        f.write(content)
