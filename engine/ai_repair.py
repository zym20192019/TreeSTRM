#!/usr/bin/env python3
"""
修复 AI 提炼失败后降级生成的 NFO（带 <tag>AI待补</tag>）。

治理流水线在文件级 AI 请求最终失败时仍会写一份兜底 NFO：打上「AI待补」标签，简介追加
「（AI提炼失败，待补全）」，并把批次记进 data/ai_failure_batches.json（status=pending_repair）。
以前没有任何代码消费这份台账；这里重新请求 AI，只原地更新标题/简介/标签，
工作室、类型、时长、文件大小与音视频流信息全部保留。
"""

import json
import os
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

AI_PENDING_TAG = "AI待补"
AI_FAIL_SUFFIX = "（AI提炼失败，待补全）"
_PENDING_MARK = f"<tag>{AI_PENDING_TAG}</tag>"

AiCall = Callable[[List[str], str], Dict[str, dict]]


def _sshfs_target(path: str) -> Optional[Tuple[str, str]]:
    """path 位于 fuse.sshfs 挂载点下时返回 (user@host, 远程绝对路径)，否则 None。"""
    real = os.path.realpath(path)
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3 or parts[2] != "fuse.sshfs":
                    continue
                source, mount_point = parts[0], parts[1].replace("\\040", " ")
                if "@" not in source or ":" not in source:
                    continue
                if real == mount_point or real.startswith(mount_point + "/"):
                    user_host, remote_base = source.split(":", 1)
                    rel = os.path.relpath(real, mount_point)
                    return user_host, (os.path.join(remote_base, rel) if rel != "." else remote_base)
    except OSError:
        pass
    return None


def _split_nul(raw: bytes) -> List[str]:
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def find_pending_nfos(cat_dir: str) -> List[str]:
    """列出 cat_dir 下所有带「AI待补」标签的 NFO。

    SSHFS 挂载点上直接 grep 十几万个小文件要逐个 RPC，极慢；与对账扫描一致，优先穿透到远端执行。
    用 -Z 以 NUL 分隔输出，名字里有换行也不会切错。
    """
    target = _sshfs_target(cat_dir)
    if target:
        user_host, remote_dir = target
        cmd = f"grep -rlZ --include='*.nfo' -F {shlex.quote(_PENDING_MARK)} {shlex.quote(remote_dir)} || true"
        try:
            raw = subprocess.check_output(
                ["ssh", "-p", "22222", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", user_host, cmd],
                timeout=900,
            )
            return sorted(os.path.join(cat_dir, os.path.relpath(p, remote_dir)) for p in _split_nul(raw))
        except Exception:
            pass
    try:
        raw = subprocess.check_output(
            ["grep", "-rlZ", "--include=*.nfo", "-F", _PENDING_MARK, cat_dir], timeout=1800
        )
        return sorted(_split_nul(raw))
    except subprocess.CalledProcessError as e:
        # grep 没有匹配时返回 1
        return sorted(_split_nul(e.output or b"")) if e.returncode == 1 else []
    except Exception:
        return []


def _set_text(root: ET.Element, tag: str, text: str):
    el = root.find(tag)
    if el is None:
        el = ET.SubElement(root, tag)
    el.text = text


def apply_ai_result(nfo_path: str, item: dict) -> bool:
    """把 AI 结果写回 NFO：更新标题/简介/标签并去掉「AI待补」，其余节点原样保留。"""
    title = str(item.get("clean_title") or "").strip()
    if not title:
        return False
    try:
        tree = ET.parse(nfo_path)
    except (ET.ParseError, OSError):
        return False
    root = tree.getroot()

    for tag in ("title", "originaltitle", "sorttitle"):
        _set_text(root, tag, title)

    old_plot = (root.findtext("plot") or "").replace(AI_FAIL_SUFFIX, "").strip()
    plot = str(item.get("summary") or "").strip() or old_plot
    for tag in ("plot", "outline"):
        _set_text(root, tag, plot)

    ai_tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    old_tag_nodes = root.findall("tag")
    old_tags = [t.text or "" for t in old_tag_nodes]
    merged = [t for t in dict.fromkeys(str(x).strip() for x in [*ai_tags, *old_tags]) if t and t != AI_PENDING_TAG]
    insert_at = list(root).index(old_tag_nodes[0]) if old_tag_nodes else len(root)
    fileinfo = root.find("fileinfo")
    if not old_tag_nodes and fileinfo is not None:
        insert_at = list(root).index(fileinfo)
    for node in old_tag_nodes:
        root.remove(node)
    for offset, tag_text in enumerate(merged):
        node = ET.Element("tag")
        node.text = tag_text
        root.insert(insert_at + offset, node)

    ET.indent(tree, space="  ")
    tmp_path = nfo_path + ".tmp"
    tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
    os.replace(tmp_path, nfo_path)
    return True


def repair_pending_nfos(
    nfo_paths: Iterable[str],
    output_root: str,
    ai_call: AiCall,
    batch_size: int = 40,
    log: Callable[[str], None] = print,
    progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    """按目录分批请求 AI（与治理流水线同一套 prompt/接口），成功的条目原地修复。"""
    groups: Dict[str, List[str]] = {}
    for p in nfo_paths:
        groups.setdefault(os.path.dirname(p), []).append(p)
    stats = {"found": sum(len(v) for v in groups.values()), "repaired": 0, "failed": 0, "repaired_paths": []}
    batch_size = max(1, int(batch_size or 40))
    for folder, nfos in groups.items():
        folder_rel = os.path.relpath(folder, output_root)
        for start in range(0, len(nfos), batch_size):
            chunk = nfos[start:start + batch_size]
            rels = [os.path.relpath(n[:-len(".nfo")] + ".strm", output_root) for n in chunk]
            try:
                result = ai_call(rels, folder_rel) or {}
            except Exception as e:
                log(f"⚠️ [AI待补修复] {folder_rel} 请求异常: {type(e).__name__}: {str(e)[:120]}")
                result = {}
            for nfo, rel in zip(chunk, rels):
                item = result.get(rel)
                if isinstance(item, dict) and apply_ai_result(nfo, item):
                    stats["repaired"] += 1
                    stats["repaired_paths"].append(rel)
                else:
                    stats["failed"] += 1
            if progress:
                progress(stats)
    return stats


def mark_ledger_repaired(ledger_file: str, repaired_rel_paths: Set[str]) -> int:
    """台账里所有文件都已修复的 pending_repair 批次标记为 repaired，返回标记条数。"""
    if not repaired_rel_paths or not os.path.exists(ledger_file):
        return 0
    try:
        with open(ledger_file, "r", encoding="utf-8") as f:
            records = json.load(f)
    except (OSError, ValueError):
        return 0
    if not isinstance(records, list):
        return 0
    marked = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for rec in records:
        if not isinstance(rec, dict) or rec.get("status") != "pending_repair":
            continue
        files = rec.get("files") or []
        if files and all(f in repaired_rel_paths for f in files):
            rec["status"] = "repaired"
            rec["repaired_at"] = now
            marked += 1
    if marked:
        tmp_path = ledger_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, ledger_file)
    return marked
