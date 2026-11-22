#!/usr/bin/env python3
"""
用当前提示词批量重写旧 NFO 的元数据（标题/制作方/作者/分类/标签/简介）。

- 只处理 TreeSTRM AI 生成的 NFO：带 <scraper>/<uniqueid>/<num>/<tmdbid>/<imdbid> 的刮削 NFO 不动
- 已带当前版本标记 <treestrm_meta>N</treestrm_meta> 的跳过，所以中断后重跑即断点续跑
- 音视频流、时长、文件大小等节点原样保留
- 按目录处理：一次目录级 AI + 若干文件级批次；目录级失败整目录跳过，文件级失败的条目保持原样
"""

import os
import re
import shlex
import subprocess
from concurrent.futures import Executor
from typing import Callable, Dict, List, Optional, Set

from engine.ai_prompts import META_MARK_TAG, META_VERSION, assemble_meta, category_of, dir_context
from engine.ai_repair import _sshfs_target, _split_nul
from engine.meta_rules import unique_title
from engine.nfo_builder import update_nfo_meta

SKIP_PATTERN = rf"<(scraper|uniqueid|num|tmdbid|imdbid)\b|<{META_MARK_TAG}>{META_VERSION}<"

DirCall = Callable[[str, List[str]], Optional[dict]]
ItemCall = Callable[[List[str], str, dict], Dict[str, dict]]


def _run_lister(cmd: List[str], remote: Optional[str], timeout: int) -> List[str]:
    if remote:
        cmd = ["ssh", "-p", "22222", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", remote, " ".join(shlex.quote(c) for c in cmd)]
    try:
        return _split_nul(subprocess.check_output(cmd, timeout=timeout))
    except subprocess.CalledProcessError as e:
        # grep -L 全部命中/无文件时返回 1
        return _split_nul(e.output or b"") if e.returncode == 1 else []


def list_regen_targets(cat_dir: str, folder_filter: str = "") -> Dict[str, List[str]]:
    """{目录: [待重写 NFO 路径]}。只收有同名 .strm 的 NFO；SSHFS 上穿透到远端执行以免逐文件 RPC。"""
    target = _sshfs_target(cat_dir)
    remote, base = (target if target else (None, cat_dir))
    grep_cmd = ["grep", "-rLZE", "--include=*.nfo", SKIP_PATTERN, base]
    find_cmd = ["find", base, "-name", "*.strm", "-print0"]
    nfos = _run_lister(grep_cmd, remote, 1800)
    strms = set(_run_lister(find_cmd, remote, 1800))
    groups: Dict[str, List[str]] = {}
    for p in sorted(nfos):
        if p[:-4] + ".strm" not in strms:
            continue
        local = os.path.join(cat_dir, os.path.relpath(p, base))
        folder = os.path.dirname(local)
        if folder_filter and not (folder == folder_filter or folder.startswith(folder_filter.rstrip("/") + "/")):
            continue
        groups.setdefault(folder, []).append(local)
    return groups


def _read_title(nfo_path: str) -> str:
    try:
        with open(nfo_path, "r", encoding="utf-8", errors="ignore") as f:
            m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", f.read(), re.S)
        return m.group(1).strip() if m else ""
    except OSError:
        return ""


def regen_folder(folder: str, nfos: List[str], output_root: str, dir_call: DirCall, item_call: ItemCall,
                 batch_size: int = 40, log: Callable[[str], None] = print, pool: Optional[Executor] = None,
                 write_pool: Optional[Executor] = None) -> dict:
    """重写一个目录下的 NFO，返回 {done, failed, skipped_dir}。
    pool: 若提供，文件级批次提交到该线程池并发请求（大目录不再串行等 AI）。
    write_pool: 若提供，NFO 写回并发执行（SSHFS 上单个文件读写+改名约 1.3s，串行是主要瓶颈）。"""
    stats = {"done": 0, "failed": 0, "skipped_dir": 0}
    folder_rel = os.path.relpath(folder, output_root)
    rels = [os.path.relpath(n[:-4] + ".strm", output_root) for n in nfos]
    ai_res = dir_call(folder_rel, rels)
    if not ai_res:
        stats["skipped_dir"] = 1
        stats["failed"] = len(nfos)
        log(f"⏭️ [NFO重写] 目录级 AI 失败，整目录保持原样: {folder_rel}")
        return stats
    ctx = dir_context(ai_res)
    category = category_of(folder_rel)
    results: Dict[str, dict] = {}
    step = max(1, batch_size)
    batches = [rels[i:i + step] for i in range(0, len(rels), step)]
    if pool is None:
        outs = [item_call(b, folder_rel, ctx) for b in batches]
    else:
        outs = [f.result() for f in [pool.submit(item_call, b, folder_rel, ctx) for b in batches]]
    for out in outs:
        results.update(out or {})
    # 失败条目保持旧标题，先占位，避免新标题与之撞名
    used: Set[str] = {_read_title(n) for n, r in zip(nfos, rels) if not isinstance(results.get(r), dict)}
    used.discard("")
    jobs = []
    for nfo, rel in zip(nfos, rels):
        item = results.get(rel)
        if not isinstance(item, dict):
            stats["failed"] += 1
            continue
        base = os.path.basename(nfo)[:-4]
        # 标题去重要按顺序做，写回可以并发
        title = unique_title(str(item.get("clean_title") or "").strip() or base, base, used)
        jobs.append((nfo, base, assemble_meta(item, ctx, category, title)))

    def write(job) -> bool:
        nfo, base, meta = job
        try:
            return update_nfo_meta(nfo, meta)
        except Exception as e:  # 单个文件写坏不影响整目录
            log(f"⚠️ [NFO重写] 写回失败: {base[:80]}: {type(e).__name__}")
            return False

    oks = list(write_pool.map(write, jobs)) if write_pool is not None else [write(j) for j in jobs]
    stats["done"] += sum(oks)
    stats["failed"] += len(oks) - sum(oks)
    return stats
