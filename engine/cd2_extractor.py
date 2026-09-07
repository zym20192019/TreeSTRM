#!/usr/bin/env python3
"""
TreeSTRM CD2 本地通道 Fast Seek 抽帧核心引擎
特性：
1. nice -n 15 + ionice -c 3 算力与 I/O 智能降权，杜绝 4K/60fps 软解压垮 CPU
2. 强制 -threads 2 约束软解线程池，平抑瞬时 Load Average
3. -ss 前置 + -noaccurate_seek 关键帧极速寻道，秒级直出
4. 黄金位 45% / 指定时间点自动定位，异常首帧保底机制
"""

import os
import time
import shutil
import subprocess
from typing import Optional, List, Dict, Callable

def extract_cover_by_cd2(
    target_path: str,
    poster_path: str,
    thumb_path: Optional[str] = None,
    seek_sec: int = 20,
    max_threads: int = 2,
    timeout_sec: int = 15
) -> bool:
    """走 CD2 本地挂载 Fast Seek 进行高效降权抽帧"""
    try:
        host_target = target_path
        if host_target.startswith('/movies/'):
            host_target = '/Movies/' + host_target[8:]

        if not os.path.exists(host_target):
            return False

        # 确保输出目录存在
        out_dir = os.path.dirname(poster_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        s_time = time.strftime('%H:%M:%S', time.gmtime(seek_sec)) if seek_sec > 0 else '00:00:05'
        
        # 基础命令构建：使用 nice 15 与 ionice 3 降权，-threads 限制
        cmd = [
            'nice', '-n', '15',
            'ionice', '-c', '3',
            'ffmpeg', '-y',
            '-threads', str(max_threads),
            '-ss', s_time,
            '-noaccurate_seek',
            '-i', host_target,
            '-frames:v', '1',
            '-vf', 'scale=min(1080\\,iw):-2',
            '-q:v', '3',
            poster_path
        ]
        
        subprocess.run(cmd, capture_output=True, timeout=timeout_sec)

        # 若长 seek 失败 (如超短片或损坏)，回退到 0.5s 处保底
        if not os.path.exists(poster_path) or os.path.getsize(poster_path) < 1000:
            cmd_fallback = [
                'nice', '-n', '15',
                'ionice', '-c', '3',
                'ffmpeg', '-y',
                '-threads', str(max_threads),
                '-ss', '00:00:00.5',
                '-noaccurate_seek',
                '-i', host_target,
                '-frames:v', '1',
                '-vf', 'scale=min(1080\\,iw):-2',
                '-q:v', '3',
                poster_path
            ]
            subprocess.run(cmd_fallback, capture_output=True, timeout=timeout_sec)

        if os.path.exists(poster_path) and os.path.getsize(poster_path) > 1000:
            if thumb_path:
                thumb_dir = os.path.dirname(thumb_path)
                if thumb_dir:
                    os.makedirs(thumb_dir, exist_ok=True)
                shutil.copyfile(poster_path, thumb_path)
            return True
        return False
    except Exception:
        return False
