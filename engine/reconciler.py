#!/usr/bin/env python3
"""
TreeSTRM 核心对账与 STRM 极速生成引擎
特性：
1. 严格 1:1 原名对应纯净 basename.strm（坚决不带中间格式后缀）
2. 外挂字幕 1:1 无损落地同步
3. 基于 Set 差集的高性能增量对账与失效索引安全回收
4. 绝对保护已有 NFO 与 海报资产，仅治理 .strm 索引
"""

import os
import io
import re
from typing import List, Tuple, Dict
from core.config import COMPREHENSIVE_VIDEO_EXTS, COMPREHENSIVE_SUBTITLE_EXTS
from core.events import push_log

def parse_tree_1to1(content: str, target_dir_name: str = "成人", include_subs: bool = True) -> Tuple[List[str], List[str], Dict[str, int]]:
    """流式合并软换行并解析目录树结构"""
    push_log("正在解析目录树并构建 1:1 资产拓扑...")
    clean_lines = []
    current_line = ''
    for raw_line in io.StringIO(str(content or "")):
        line = str(raw_line or "").replace("\ufeff", "").rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith('|') or line.startswith('﻿|'):
            if current_line:
                clean_lines.append(current_line)
            current_line = line
        else:
            current_line += ' ' + line.strip()
    if current_line:
        clean_lines.append(current_line)

    path_stack = {}
    matched_videos = []
    matched_subs = []
    ext_stats = {}

    for line in clean_lines:
        level = line.count("|")
        clean_name = re.sub(r"^[|\s—\-]+", "", line).strip()
        if not clean_name:
            continue

        for stale_level in [k for k in path_stack.keys() if k > level]:
            path_stack.pop(stale_level, None)
        path_stack[level] = clean_name

        parts = [path_stack[d] for d in range(level + 1) if d in path_stack]
        if parts and parts[0] in ['根目录', 'ROOT', '']:
            parts = parts[1:]
        if not parts:
            continue

        if target_dir_name and parts[0] != target_dir_name:
            continue

        _, ext = os.path.splitext(clean_name.lower())
        rel_path = "/".join(parts)

        if ext in COMPREHENSIVE_VIDEO_EXTS and not clean_name.endswith('.txt'):
            matched_videos.append(rel_path)
            ext_stats[ext] = ext_stats.get(ext, 0) + 1
        elif include_subs and ext in COMPREHENSIVE_SUBTITLE_EXTS:
            matched_subs.append(rel_path)
            ext_stats[ext] = ext_stats.get(ext, 0) + 1

    if len(matched_videos) == 0:
        raise RuntimeError("目录树解析未提取到有效视频，触发安全熔断，中止同步以防误删！")

    push_log(f"解析成功！全量识别到正片视频: {len(matched_videos)} 部，外挂字幕: {len(matched_subs)} 轨")
    return matched_videos, matched_subs, ext_stats

def sync_strms_pure_1to1(
    video_paths: List[str],
    sub_paths: List[str],
    output_dir: str,
    strm_prefix: str,
    auto_cleanup: bool = True,
    progress_callback = None
) -> Tuple[int, int, int]:
    """1:1 纯净生成 STRM 播放索引文件"""
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    strm_prefix = str(strm_prefix or "").rstrip('/')

    push_log("正在构建本次云端目标 STRM 完整集合...")
    expected_strms = {}

    for rel_path in video_paths:
        dirname = os.path.dirname(rel_path)
        filename = os.path.basename(rel_path)
        basename, _ = os.path.splitext(filename)
        strm_filename = f"{basename}.strm"
        target_path = os.path.join(output_dir, dirname, strm_filename) if dirname else os.path.join(output_dir, strm_filename)
        content = f"{strm_prefix}/{rel_path.lstrip('/')}"
        expected_strms[target_path] = content

    for rel_path in sub_paths:
        dirname = os.path.dirname(rel_path)
        filename = os.path.basename(rel_path)
        target_path = os.path.join(output_dir, dirname, filename) if dirname else os.path.join(output_dir, filename)
        content = f"{strm_prefix}/{rel_path.lstrip('/')}"
        expected_strms[target_path] = content

    push_log(f"云端有效 STRM 应有: {len(expected_strms)} 个")

    existing_local_strms = set()
    if os.path.exists(output_dir):
        for root, _, files in os.walk(output_dir):
            for f in files:
                if f.endswith('.strm'):
                    existing_local_strms.add(os.path.join(root, f))

    push_log(f"本地当前已有 STRM 文件: {len(existing_local_strms)} 个")

    v_created = 0
    v_skipped = 0
    v_deleted = 0

    if auto_cleanup and len(expected_strms) > 0:
        orphan_strms = existing_local_strms - set(expected_strms.keys())
        if orphan_strms:
            push_log(f"发现 {len(orphan_strms)} 个云端已删除的过期 STRM，正在安全清理索引...")
            for old_p in orphan_strms:
                try:
                    os.remove(old_p)
                    v_deleted += 1
                except Exception:
                    pass
            push_log(f"已成功清理 {v_deleted} 个过期无效 STRM 索引文件（元数据海报保持完整）！")

    total_to_process = len(expected_strms)
    batch_count = 0

    for target_path, content in expected_strms.items():
        batch_count += 1
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)

        if target_path not in existing_local_strms:
            try:
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(content)
                v_created += 1
            except Exception:
                pass
        else:
            v_skipped += 1

        if batch_count % 30000 == 0 or batch_count == total_to_process:
            pct = round(batch_count / total_to_process * 100, 1)
            push_log(f"STRM 索引生成进度: {batch_count}/{total_to_process} ({pct}%) - 已新建: {v_created}, 跳过已有: {v_skipped}")
            if progress_callback:
                progress_callback(batch_count, total_to_process, v_created, v_skipped, v_deleted)

    push_log(f"🎉 全量 STRM 生成完成！新增: {v_created}，跳过已有: {v_skipped}，清理过期: {v_deleted}")
    return v_created, v_skipped, v_deleted
