#!/usr/bin/env python3
"""
【1 次目录树导出，按专区自动分流与排除】算法模块
"""

import os, io, re
from typing import Dict, List, Set

TARGET_SECTIONS = {
    "FC2": {
        "output_dir": "/Movies/SmartStrms/FC2",
        "prefix": "/movies/CloudDrive/115/成人/FC2"
    },
    "AV女优": {
        "output_dir": "/Movies/SmartStrms/AV女优",
        "prefix": "/movies/CloudDrive/115/成人/AV女优"
    },
    "无码刮削": {
        "output_dir": "/Movies/SmartStrms/无码刮削",
        "prefix": "/movies/CloudDrive/115/成人/无码刮削"
    },
    "我爱的AV": {
        "output_dir": "/Movies/SmartStrms/我爱的AV",
        "prefix": "/movies/CloudDrive/115/成人/我爱的AV"
    },
    "欧美刮削": {
        "output_dir": "/Movies/SmartStrms/欧美刮削",
        "prefix": "/movies/CloudDrive/115/成人/欧美刮削"
    },
    "欧美刮削_01": {
        "output_dir": "/Movies/SmartStrms/欧美刮削_01",
        "prefix": "/movies/CloudDrive/115/成人/欧美刮削_01"
    }
}

DEFAULT_VIDEO_EXTS = {
    '.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.ts', '.m2ts', 
    '.iso', '.rmvb', '.webm', '.m4v'
}

def parse_and_route_chengren_tree(content: str) -> Dict[str, List[str]]:
    """
    流式深度栈解析整棵 /成人 目录树
    自动分流到 6 个专区，自动排除庞大的「舞蹈」及其他非关注目录
    返回: { "FC2": ["卖家/xxx.mp4", ...], "AV女优": ["一条莉音/xxx.mp4", ...] }
    """
    path_stack = {}
    routed_videos = {sec: [] for sec in TARGET_SECTIONS.keys()}
    
    for raw_line in io.StringIO(str(content or "")):
        line = str(raw_line or "").replace("\ufeff", "").rstrip("\r\n")
        if not line.strip():
            continue
        level = line.count("|")
        clean_name = re.sub(r"^[|\s—\-]+", "", line).strip()
        if not clean_name:
            continue
            
        for stale_level in [k for k in path_stack.keys() if k > level]:
            path_stack.pop(stale_level, None)
        path_stack[level] = clean_name
        
        _, ext = os.path.splitext(clean_name.lower())
        if ext in DEFAULT_VIDEO_EXTS and not clean_name.endswith('.txt'):
            full_parts = [path_stack[d] for d in range(level + 1) if d in path_stack]
            if len(full_parts) >= 2:
                # full_parts[0] 是 "成人"
                section_name = full_parts[1]
                if section_name in TARGET_SECTIONS:
                    # 专区内的相对路径
                    rel_sub_path = "/".join(full_parts[2:]) if len(full_parts) > 2 else full_parts[1]
                    routed_videos[section_name].append(rel_sub_path)
                    
    return routed_videos
