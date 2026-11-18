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
import subprocess
import urllib.parse
from typing import Callable, List, Optional, Tuple, Dict, Set
from core.config import COMPREHENSIVE_VIDEO_EXTS, COMPREHENSIVE_SUBTITLE_EXTS
from core.events import push_log

# 过期 STRM 清理熔断：单次待删数量 >= MIN_COUNT 且占本地比例 > MAX_RATIO 时拒绝清理
ORPHAN_GUARD_MIN_COUNT = 500
ORPHAN_GUARD_MAX_RATIO = 0.3

# progress_callback(已处理, 总数, 新建, 跳过, 删除)
ProgressCallback = Callable[[int, int, int, int, int], None]

# 115 目录树结构前缀：根行为 "|——"，其余每层一个 "| "，最后接 "|-"，如 "| | |-名字"。
_TREE_PREFIX_RE = re.compile(r"^(?:\| )*\|(?:——|-)")


def _tree_entry_name(line: str) -> str:
    """只剥掉 115 树格式的结构前缀（恰好一个 "|-" 或根行的 "|——"），名字本身原样保留。

    115 允许名字首尾带空格、以 "-"/"—" 开头；Alist 按真实名字寻址，
    对名字做任何 strip 都会让 STRM 路径和云端对不上，导致播放失败。
    """
    value = str(line or "").replace("\ufeff", "").rstrip("\r\n")
    m = _TREE_PREFIX_RE.match(value)
    if m:
        return value[m.end():]
    # 非标准行（理论上不出现）：退回宽松解析
    value = re.sub(r"^[|\s]*", "", value)
    value = re.sub(r"^[—\-\─\–]+", "", value)
    return value.strip()


def _tree_line_level(line: str) -> int:
    """层级只按结构前缀里的 "|" 计算，避免名字里自带的 "|" 把层级算错。"""
    value = str(line or "").replace("\ufeff", "")
    m = _TREE_PREFIX_RE.match(value)
    return m.group(0).count("|") if m else value.count("|")


def parse_tree_1to1(content: str, target_dir_name: str = "成人", include_subs: bool = True) -> Tuple[List[str], List[str], Dict[str, int]]:
    """流式合并软换行并解析目录树结构"""
    push_log("正在解析目录树并构建 1:1 资产拓扑...")
    clean_lines = []
    wrapped_names: List[str] = []
    current_line = ''
    for raw_line in io.StringIO(str(content or "")):
        line = str(raw_line or "").replace("\ufeff", "").rstrip("\r\n")
        if not line:
            continue
        if line.startswith('|') or line.startswith('﻿|'):
            if current_line:
                clean_lines.append(current_line)
            current_line = line
        else:
            # 115 名字里含真实换行时，目录树会把它拆成多行；这里用空格拼回同一个名字。
            # 拼出来的名字与云端真实名字不同，生成的 STRM 在 Alist 上找不到文件，必须去 115 改名。
            wrapped_names.append((current_line + "⏎" + line)[-120:])
            current_line += " " + line.lstrip(" ")
    if current_line:
        clean_lines.append(current_line)
    if wrapped_names:
        push_log(f"⚠️ 发现 {len(wrapped_names)} 个 115 名字含换行符，对应 STRM 将无法播放，请在 115 把换行改成空格后重新同步：")
        for w in wrapped_names[:20]:
            push_log(f"   ↳ {w}")

    line_levels = [_tree_line_level(l) for l in clean_lines]
    num_lines = len(clean_lines)

    path_stack = {}
    matched_videos = []
    matched_subs = []
    ext_stats = {}

    for i in range(num_lines):
        line = clean_lines[i]
        level = line_levels[i]
        clean_name = _tree_entry_name(line)
        if not clean_name:
            continue

        for stale_level in [k for k in path_stack.keys() if k > level]:
            path_stack.pop(stale_level, None)

        # 核心修复：115 目录树把单层目录/文件名中的全角或原生斜杠打印为半角 /
        # 但在文件系统路径中，单层名字内部的 / 必须替换为全角 ／ 避免被误切分成多级不存在的虚拟子目录
        clean_name_escaped = clean_name.replace('/', '／')
        path_stack[level] = clean_name_escaped

        # 智能判定：若下一行层级更深，说明当前项是目录（避免类似 1(2).mp4 命名的历史空文件夹被误当成视频生成 STRM）
        if i + 1 < num_lines and line_levels[i + 1] > level:
            continue

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

def scan_existing_strms_fast(output_dir: str) -> Set[str]:
    """
    极速盘点本地现有 STRM 集合。
    针对 SSHFS 等跨洋网络挂载点，自动穿透使用 SSH 远程快速执行 find，
    将 19 万文件的遍历耗时从 20 分钟压至 5 秒内，杜绝网络文件系统逐个目录 RPC 卡死。
    """
    real_path = os.path.realpath(output_dir)
    is_sshfs = False
    sshfs_target = None
    mount_root = real_path

    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[2] == "fuse.sshfs":
                    mp = parts[1]
                    if real_path == mp or real_path.startswith(mp + "/"):
                        is_sshfs = True
                        sshfs_target = parts[0]
                        mount_root = mp
                        break
    except Exception:
        pass

    if is_sshfs and sshfs_target and "@" in sshfs_target and ":" in sshfs_target:
        try:
            user_host, remote_base = sshfs_target.split(":", 1)
            rel_sub = os.path.relpath(real_path, mount_root)
            remote_search_dir = os.path.join(remote_base, rel_sub) if rel_sub != "." else remote_base
            ssh_cmd = [
                "ssh", "-p", "22222",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                user_host,
                f"find {remote_search_dir} -name '*.strm'"
            ]
            res = subprocess.check_output(ssh_cmd, text=True, timeout=30)
            remote_paths = res.strip().split("\n")
            local_strms = set()
            for rp in remote_paths:
                if not rp:
                    continue
                rel = os.path.relpath(rp, remote_search_dir)
                local_strms.add(os.path.join(output_dir, rel))
            push_log(f"⚡ [SSHFS 加速] 穿透远程执行 find，瞬间完成本地盘点！共识别到 {len(local_strms)} 个 STRM")
            return local_strms
        except Exception as e:
            push_log(f"⚠️ 远程 SSHFS 加速扫描失败 ({e})，降级为常规扫描...")

    try:
        res = subprocess.check_output(["find", real_path, "-name", "*.strm"], text=True, timeout=120)
        strms = set()
        for p in res.strip().split("\n"):
            if not p:
                continue
            rel = os.path.relpath(p, real_path)
            strms.add(os.path.join(output_dir, rel))
        return strms
    except Exception:
        local_strms = set()
        for root, _, files in os.walk(real_path):
            for f in files:
                if f.endswith('.strm'):
                    local_strms.add(os.path.join(root, f))
        return local_strms


# 同目录同主名视频的「主文件」优先级：保持 <主名>.strm 指向它，其余用 <完整文件名>.strm
_PRIMARY_EXT_PRIORITY = (".mp4", ".mkv", ".m4v", ".mov", ".ts", ".avi", ".wmv", ".flv", ".webm")


def _strm_content(strm_prefix: str, rel_path: str) -> str:
    encoded_rel_path = "/".join(urllib.parse.quote(part) for part in rel_path.lstrip("/").split("/"))
    return f"{strm_prefix}/{encoded_rel_path}"


def _read_strm(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return None


def plan_expected_strms(
    video_paths: List[str],
    output_dir: str,
    strm_prefix: str,
    existing_local_strms: Set[str],
) -> Tuple[Dict[str, str], Set[str]]:
    """计算目标 STRM 集合 {本地路径: 内容}，以及需要改写内容的已有 STRM。

    115 同一目录下常有 a.mp4 + a.mov 这类同主名不同格式的视频，以前它们抢同一个 a.strm，
    只有一个能在媒体库里播放。现在：
      - 主文件保持 a.strm（优先沿用本地 a.strm 当前指向的那个，避免已有 NFO/海报失配）；
      - 其余文件生成 a.mov.strm 这样的「完整文件名.strm」；
      - a.strm 指向的文件已不在云端时改写为新的主文件（计入「修复路径」）。
    """
    groups: Dict[Tuple[str, str], List[str]] = {}
    for rel_path in video_paths:
        filename = os.path.basename(rel_path)
        basename, raw_ext = os.path.splitext(filename)
        # 安全防御拦截：如果扩展名属于字幕或非视频，坚决丢弃
        if raw_ext.lower() in COMPREHENSIVE_SUBTITLE_EXTS:
            continue
        key = (os.path.dirname(rel_path), basename)
        members = groups.setdefault(key, [])
        if rel_path not in members:
            members.append(rel_path)

    def target_of(dirname: str, strm_filename: str) -> str:
        return os.path.join(output_dir, dirname, strm_filename) if dirname else os.path.join(output_dir, strm_filename)

    def ext_rank(rel_path: str) -> Tuple[int, str]:
        ext = os.path.splitext(rel_path)[1].lower()
        rank = _PRIMARY_EXT_PRIORITY.index(ext) if ext in _PRIMARY_EXT_PRIORITY else len(_PRIMARY_EXT_PRIORITY)
        return rank, rel_path

    expected: Dict[str, str] = {}
    repair_targets: Set[str] = set()
    multi_groups = 0
    alt_count = 0
    alt_conflicts: List[str] = []
    deferred_alts: List[Tuple[str, str]] = []

    for (dirname, basename), members in groups.items():
        base_target = target_of(dirname, f"{basename}.strm")
        if len(members) == 1:
            expected[base_target] = _strm_content(strm_prefix, members[0])
            continue

        multi_groups += 1
        contents = {m: _strm_content(strm_prefix, m) for m in members}
        primary = None
        if base_target in existing_local_strms:
            current = _read_strm(base_target)
            primary = next((m for m in members if contents[m] == current), None)
            if primary is None:
                repair_targets.add(base_target)
        if primary is None:
            primary = min(members, key=ext_rank)
        expected[base_target] = contents[primary]
        for m in members:
            if m != primary:
                deferred_alts.append((target_of(dirname, f"{os.path.basename(m)}.strm"), contents[m]))

    # 「完整文件名.strm」放在最后登记，绝不覆盖任何主名 STRM（如 a.mp4.mkv 的主名恰好也是 a.mp4.strm）
    for alt_target, content in deferred_alts:
        if alt_target in expected:
            alt_conflicts.append(alt_target)
            continue
        expected[alt_target] = content
        alt_count += 1

    if multi_groups:
        push_log(f"同目录同主名、不同格式的视频 {multi_groups} 组，额外生成 {alt_count} 个「完整文件名.strm」索引，保证每个视频都能播放")
    if repair_targets:
        push_log(f"{len(repair_targets)} 个同名组的主 STRM 指向的文件已不在云端，将改写为现存文件")
    for c in alt_conflicts[:10]:
        push_log(f"⚠️ 同名索引冲突，跳过: {c}")
    return expected, repair_targets


def sync_strms_pure_1to1(
    video_paths: List[str],
    sub_paths: List[str],
    output_dir: str,
    strm_prefix: str,
    auto_cleanup: bool = True,
    progress_callback: Optional[ProgressCallback] = None
) -> Tuple[int, int, int]:
    """1:1 纯净生成 STRM 播放索引文件"""
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    strm_prefix = str(strm_prefix or "").rstrip('/')

    existing_local_strms = scan_existing_strms_fast(output_dir)
    push_log(f"本地当前已有 STRM 文件: {len(existing_local_strms)} 个")

    push_log("正在构建本次云端目标 STRM 完整集合...")
    expected_strms, repair_targets = plan_expected_strms(video_paths, output_dir, strm_prefix, existing_local_strms)
    push_log(f"云端有效 STRM 应有: {len(expected_strms)} 个")

    v_created = 0
    v_skipped = 0
    v_deleted = 0
    v_repaired = 0

    if auto_cleanup and len(expected_strms) > 0:
        orphan_strms = existing_local_strms - set(expected_strms.keys())
        orphan_ratio = len(orphan_strms) / len(existing_local_strms) if existing_local_strms else 0.0
        if len(orphan_strms) >= ORPHAN_GUARD_MIN_COUNT and orphan_ratio > ORPHAN_GUARD_MAX_RATIO:
            # 目录树下载截断/解码丢行时，解析结果会偏少，此时清理会误删大量有效 STRM
            push_log(f"🛑 安全熔断：待清理过期 STRM {len(orphan_strms)} 个，占本地 {round(orphan_ratio * 100, 1)}%，"
                     f"超过 {int(ORPHAN_GUARD_MAX_RATIO * 100)}% 阈值，疑似目录树不完整，本次跳过清理！")
            orphan_strms = set()
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

    push_log("开始增量写入 STRM 播放索引...")

    for target_path, content in expected_strms.items():
        batch_count += 1

        if target_path not in existing_local_strms:
            target_dir = os.path.dirname(target_path)
            os.makedirs(target_dir, exist_ok=True)
            try:
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(content)
                v_created += 1
            except Exception:
                pass
        elif target_path in repair_targets:
            try:
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(content)
                v_repaired += 1
            except Exception:
                pass
        else:
            v_skipped += 1

        if progress_callback:
            progress_callback(batch_count, total_to_process, v_created, v_skipped, v_deleted)
        if batch_count % 30000 == 0 or batch_count == total_to_process:
            pct = round(batch_count / total_to_process * 100, 1)
            push_log(f"STRM 索引生成进度: {batch_count}/{total_to_process} ({pct}%) - 已新建: {v_created}, 跳过已有: {v_skipped}")

    push_log(f"🎉 全量 STRM 生成完成！新增: {v_created}，修复路径: {v_repaired}，跳过已有: {v_skipped}，清理过期: {v_deleted}")
    return v_created, v_skipped, v_deleted
