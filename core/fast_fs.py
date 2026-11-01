#!/usr/bin/env python3
"""
TreeSTRM 跨存储/跨洋网络文件系统 (SSHFS/FUSE) 极速扫描穿透通用辅助模块
"""

import os
import subprocess
import json
from typing import Dict, List, Optional, Tuple, Any

def get_sshfs_mount_info(path: str) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    检查路径是否位于 SSHFS 挂载点下。
    返回: (is_sshfs, sshfs_target, mount_point, rel_path_under_mount)
    """
    real_path = os.path.realpath(path)
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[2] == "fuse.sshfs":
                    mp = parts[1]
                    if real_path == mp or real_path.startswith(mp + "/"):
                        sshfs_target = parts[0]
                        rel_sub = os.path.relpath(real_path, mp)
                        return True, sshfs_target, mp, (rel_sub if rel_sub != "." else "")
    except Exception:
        pass
    return False, None, None, None


def fast_walk_files(target_dir: str, timeout: int = 40) -> Dict[str, List[str]]:
    """
    极速递归遍历目录。
    若为 SSHFS 挂载点，自动穿透执行远端 Python 本地 walk 并序列化返回。
    返回: {本地绝对路径: [该目录下的文件名列表, ...]}
    """
    is_sshfs, sshfs_target, mount_point, rel_sub = get_sshfs_mount_info(target_dir)

    if is_sshfs and sshfs_target and "@" in sshfs_target and ":" in sshfs_target:
        try:
            user_host, remote_base = sshfs_target.split(":", 1)
            remote_search_dir = os.path.join(remote_base, rel_sub) if rel_sub else remote_base
            remote_code = f"""
import os, json
base = '{remote_search_dir}'
res = {{}}
for root, _, files in os.walk(base):
    if files:
        res[root] = files
print('__FAST_JSON__')
print(json.dumps(res))
"""
            ssh_cmd = [
                "ssh", "-p", "22222",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                user_host,
                f"python3 -c \"{remote_code}\""
            ]
            output = subprocess.check_output(ssh_cmd, text=True, timeout=timeout)
            if "__FAST_JSON__" in output:
                json_str = output.split("__FAST_JSON__")[1].strip()
                data = json.loads(json_str)
                result = {}
                for r_root, files in data.items():
                    rel = os.path.relpath(r_root, remote_search_dir)
                    local_root = os.path.join(target_dir, rel) if rel != "." else target_dir
                    result[local_root] = files
                return result
        except Exception:
            pass

    # 常规本地兜底
    result = {}
    for root, _, files in os.walk(target_dir):
        if files:
            result[root] = files
    return result
