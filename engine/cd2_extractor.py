#!/usr/bin/env python3
"""
TreeSTRM CD2 本地通道 Fast Seek 抽帧核心引擎
特性：
1. nice -n 15 + ionice -c 3 算力与 I/O 智能降权，杜绝 4K/60fps 软解压垮 CPU
2. 强制 -threads 2 约束软解线程池，平抑瞬时 Load Average
3. -ss 前置 + -noaccurate_seek 关键帧极速寻道，秒级直出
4. 一体化探针 (probesize 限制): 1.2s 内顺带获取文件大小、时长、视音频流并定位总时长/2
5. 自动反哺回写 NFO streamdetails，阻断 Emby 扫盘物理探针海啸
"""

import os
import time
import json
import shutil
import subprocess
from typing import Optional, Dict, Tuple
from xml.etree import ElementTree as ET


def probe_media_info(video_path: str, timeout_sec: int = 18) -> Dict:
    """利用轻量 ffprobe 提取全量音视频媒体信息与文件大小，内置重试与强有效性校验"""
    info = {
        "filesize": 0,
        "duration": 0.0,
        "video": {},
        "audio": {},
        "valid": False
    }
    try:
        if os.path.exists(video_path):
            info["filesize"] = os.path.getsize(video_path)
    except Exception:
        pass

    cmd = [
        'nice', '-n', '15',
        'ionice', '-c', '3',
        'ffprobe', '-v', 'quiet',
        '-probesize', '1000000',
        '-analyzeduration', '1000000',
        '-print_format', 'json',
        '-show_format',
        '-show_streams',
        video_path
    ]

    for attempt in range(2):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                fmt = data.get("format", {})
                try:
                    info["duration"] = float(fmt.get("duration", 0.0))
                except Exception:
                    info["duration"] = 0.0

                if info["filesize"] == 0:
                    try:
                        info["filesize"] = int(fmt.get("size", 0))
                    except Exception:
                        pass

                for s in data.get("streams", []):
                    ctype = s.get("codec_type")
                    if ctype == "video" and not info["video"]:
                        w = s.get("width")
                        h = s.get("height")
                        dur_s = s.get("duration")
                        if (not info["duration"] or info["duration"] == 0.0) and dur_s:
                            try:
                                info["duration"] = float(dur_s)
                            except Exception:
                                pass
                        if w and h:
                            info["video"] = {
                                "codec": s.get("codec_name", "h264"),
                                "width": int(w),
                                "height": int(h),
                                "aspect": s.get("display_aspect_ratio", "")
                            }
                    elif ctype == "audio" and not info["audio"]:
                        info["audio"] = {
                            "codec": s.get("codec_name", "aac"),
                            "channels": s.get("channels", 2),
                            "samplingrate": s.get("sample_rate", "48000")
                        }

                if info["video"].get("width", 0) > 0 and info["duration"] > 0:
                    info["valid"] = True
                    break
        except Exception:
            time.sleep(1)

    return info


def update_nfo_with_streamdetails(nfo_path: str, media_info: Dict) -> bool:
    """将探针获取到的完整音视频流信息、时长、文件大小顺手回写进 NFO。若探针数据无效严禁写入，杜绝空壳污染"""
    if not nfo_path or not os.path.exists(nfo_path) or not media_info or not media_info.get("valid"):
        return False

    v = media_info.get("video", {})
    dur = media_info.get("duration", 0.0)
    if not v or v.get("width", 0) <= 0 or dur <= 0:
        return False

    try:
        tree = ET.parse(nfo_path)
        root = tree.getroot()

        rt_elem = root.find("runtime")
        if rt_elem is None:
            rt_elem = ET.SubElement(root, "runtime")
        rt_elem.text = str(max(1, round(dur / 60)))

        fs = media_info.get("filesize", 0)
        if fs > 0:
            fs_elem = root.find("filesize")
            if fs_elem is None:
                fs_elem = ET.SubElement(root, "filesize")
            fs_elem.text = str(fs)

        fileinfo = root.find("fileinfo")
        if fileinfo is None:
            fileinfo = ET.SubElement(root, "fileinfo")
        streamdetails = fileinfo.find("streamdetails")
        if streamdetails is None:
            streamdetails = ET.SubElement(fileinfo, "streamdetails")

        # 清除旧的 video/audio 节点，重新写满
        for child in list(streamdetails):
            streamdetails.remove(child)

        ve = ET.SubElement(streamdetails, "video")
        if v.get("codec"): ET.SubElement(ve, "codec").text = str(v["codec"])
        if v.get("width"): ET.SubElement(ve, "width").text = str(v["width"])
        if v.get("height"): ET.SubElement(ve, "height").text = str(v["height"])
        if v.get("aspect"): ET.SubElement(ve, "aspect").text = str(v["aspect"])
        ET.SubElement(ve, "durationinseconds").text = str(round(dur))

        a = media_info.get("audio", {})
        if a:
            ae = ET.SubElement(streamdetails, "audio")
            if a.get("codec"): ET.SubElement(ae, "codec").text = str(a["codec"])
            if a.get("channels"): ET.SubElement(ae, "channels").text = str(a["channels"])
            if a.get("samplingrate"): ET.SubElement(ae, "samplingrate").text = str(a["samplingrate"])

        ET.indent(tree, space="  ", level=0)
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        return True
    except Exception as e:
        return False


def extract_cover_by_cd2(
    target_path: str,
    poster_path: str,
    thumb_path: Optional[str] = None,
    seek_sec: int = 0,
    nfo_path: Optional[str] = None,
    max_threads: int = 2,
    timeout_sec: int = 35
) -> bool:
    """走 CD2 本地挂载 Fast Seek 进行高效降权抽帧，顺便提取完整元数据反哺 NFO"""
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

        # 1. 轻量级快速探针 (顺带读取大小、总时长、视音频流)
        media_info = probe_media_info(host_target, timeout_sec=8)

        # 2. 黄金截取点策略：
        # 对于 .webm 格式 (缺乏索引，远程seek极慢)，优先截取片头 3-5 秒，避免拉取数百兆远程数据
        # 对于 .mp4/.mkv 等带头索引格式，保留 总时长/2 居中黄金帧
        ext = os.path.splitext(host_target)[1].lower()
        real_duration = media_info.get("duration", 0.0)
        
        if seek_sec > 0:
            target_seek = seek_sec
        elif ext == '.webm':
            target_seek = 3
        elif real_duration > 10:
            target_seek = int(real_duration / 2)
        else:
            target_seek = 3

        s_time = time.strftime('%H:%M:%S', time.gmtime(target_seek))

        # 3. 降权抽帧
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

        # 若截取失败，回退到片头 0.5s 保底
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

        # 4. 验证封面并顺带回写 NFO
        if os.path.exists(poster_path) and os.path.getsize(poster_path) > 1000:
            if thumb_path:
                thumb_dir = os.path.dirname(thumb_path)
                if thumb_dir:
                    os.makedirs(thumb_dir, exist_ok=True)
                shutil.copyfile(poster_path, thumb_path)

            # 顺手将探针获取到的完整音视频流与大小写入 NFO
            if nfo_path:
                if not os.path.exists(nfo_path):
                    # 如果当前还未生成 NFO，生成一个合法的极简骨架 NFO，确保流信息不丢失且不破坏后续 AI 覆盖
                    base_title = os.path.splitext(os.path.basename(nfo_path))[0]
                    minimal_xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
<movie>
  <title><![CDATA[{base_title}]]></title>
</movie>"""
                    try:
                        with open(nfo_path, "w", encoding="utf-8") as f:
                            f.write(minimal_xml)
                    except Exception:
                        pass
                if os.path.exists(nfo_path):
                    update_nfo_with_streamdetails(nfo_path, media_info)

            return True
        return False
    except Exception:
        return False
