import os, sys, re, time, json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional

SUBTITLE_EXTS = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.sup', '.idx', '.smi'}

# 预编译正则，避免循环内重复解析
IMAGE_POSTER_RE = re.compile(r'-(poster|thumb)\.(jpg|png|jpeg|webp|gif)$', re.IGNORECASE)

def scan_single_category(cat_dir: str) -> Dict[str, Any]:
    cat_dir = os.path.abspath(cat_dir)
    cat_name = os.path.basename(cat_dir)
    if not os.path.exists(cat_dir):
        return {
            "name": cat_name,
            "path": cat_dir,
            "video_count": 0,
            "sub_count": 0,
            "cover_count": 0,
            "nfo_count": 0,
            "cover_pct": 100,
            "nfo_pct": 100
        }

    v_set = set()
    c_set = set()
    n_set = set()
    sub_count = 0

    # 极速内存扫盘：不打开文件，零磁盘读取开销
    for root, _, files in os.walk(cat_dir):
        for f in files:
            if f.endswith('.strm'):
                base = f[:-5]
                # 仅当文件名显式包含字幕特征时判定为字幕，免去逐个 open() 读取
                lower_base = base.lower()
                if any(lower_base.endswith(ext) for ext in SUBTITLE_EXTS):
                    sub_count += 1
                    continue
                v_set.add(os.path.join(root, base))
            elif f.endswith('.nfo'):
                n_set.add(os.path.join(root, f[:-4]))
            elif IMAGE_POSTER_RE.search(f):
                m_base = IMAGE_POSTER_RE.sub('', f)
                c_set.add(os.path.join(root, m_base))
            elif any(f.lower().endswith(ext) for ext in SUBTITLE_EXTS):
                sub_count += 1

    v_count = len(v_set)
    c_count = len(c_set.intersection(v_set))
    n_count = len(n_set.intersection(v_set))
    c_pct = round(c_count / v_count * 100) if v_count > 0 else 100
    n_pct = round(n_count / v_count * 100) if v_count > 0 else 100

    return {
        "name": cat_name,
        "path": cat_dir,
        "video_count": v_count,
        "sub_count": sub_count,
        "cover_count": c_count,
        "nfo_count": n_count,
        "cover_pct": c_pct,
        "nfo_pct": n_pct
    }

def scan_all_categories(root_dir: str = "/Movies/TreeStrms/成人", max_workers: int = 8) -> Dict[str, Dict[str, Any]]:
    root_dir = os.path.abspath(root_dir)
    if not os.path.exists(root_dir):
        return {}
    
    subdirs = sorted([d for d in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, d))])
    cat_paths = [os.path.join(root_dir, d) for d in subdirs]
    
    result = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        scanned_list = list(pool.map(scan_single_category, cat_paths))
        
    for item in scanned_list:
        result[item["name"]] = item
    return result

def sync_cache_to_disk(cache_path: str = "/opt/treestrm/data/categories_cache.json", root_dir: str = "/Movies/TreeStrms/成人") -> Dict[str, Dict[str, Any]]:
    stats = scan_all_categories(root_dir)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    temp_path = cache_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, cache_path)
    return stats
