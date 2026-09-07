#!/usr/bin/env python3
"""
TreeSTRM 孤立失效资产安全排查与清理引擎
特性：
1. 严格以 .strm 实体为基准对齐
2. 绝对保护 -fanart.gif, -thumb.gif, -landscape.jpg 等动图与多媒体辅助资产（若 strm 存在坚决保留）
3. 仅清理确实已无对应 .strm 的孤立 .nfo 和孤立图片
4. 详尽的日志与清理数量审计
"""

import os
from typing import Dict, Any
from core.events import push_log
from core.config import load_config

def clean_orphan_metadata_for_category(category_name: str, base_dir: str = "/Movies/TreeStrms") -> Dict[str, Any]:
    """对指定专区安全清理无对应 strm 的孤立废弃元数据"""
    cfg = load_config()
    target_dir = os.path.join(cfg.get("default_output", base_dir), "成人", category_name)
    if not os.path.exists(target_dir):
        return {"deleted_nfo": 0, "deleted_posters": 0, "deleted_thumbs": 0, "total": 0}

    push_log(f"======== 🧹 开始对专区【{category_name}】执行孤立失效元数据清理 ========")
    del_nfo = 0
    del_poster = 0
    del_thumb = 0

    for root, dirs, files in os.walk(target_dir):
        strms = set()
        for f in files:
            if f.endswith('.strm'):
                strms.add(f[:-5])

        for f in files:
            p = os.path.join(root, f)
            
            # 1. 孤立 NFO
            if f.endswith('.nfo'):
                base = f[:-4]
                if base not in strms:
                    try:
                        os.remove(p)
                        del_nfo += 1
                    except Exception:
                        pass
                        
            # 2. 孤立 Poster
            elif f.endswith('-poster.jpg') or f.endswith('-poster.jpeg'):
                base = f[:-11] if f.endswith('-poster.jpg') else f[:-12]
                if base not in strms:
                    try:
                        os.remove(p)
                        del_poster += 1
                    except Exception:
                        pass

            # 3. 孤立 Thumb
            elif f.endswith('-thumb.jpg') or f.endswith('-thumb.jpeg'):
                base = f[:-10] if f.endswith('-thumb.jpg') else f[:-11]
                if base not in strms:
                    try:
                        os.remove(p)
                        del_thumb += 1
                    except Exception:
                        pass

    total_cleaned = del_nfo + del_poster + del_thumb
    push_log(f"✅ 专区【{category_name}】失效元数据清理完毕！共删除孤立文件: {total_cleaned} 个 (NFO: -{del_nfo}, 海报: -{del_poster}, 缩略图: -{del_thumb})")
    return {
        "deleted_nfo": del_nfo,
        "deleted_posters": del_poster,
        "deleted_thumbs": del_thumb,
        "total": total_cleaned
    }
