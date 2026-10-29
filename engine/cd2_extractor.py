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
import re
import json
import time
import shutil
import subprocess
import urllib.parse
import unicodedata
from typing import Optional, Dict, Tuple
from xml.etree import ElementTree as ET


def map_to_cd2_path(target: str, cd2_mount_path: Optional[str] = None, stream_prefix: Optional[str] = None) -> str:
    """将 STRM 中的远程播放 URL（URL 编码）映射为本地 CD2 物理挂载路径"""
    mount_root = cd2_mount_path or "/Movies/CloudDrive/115"
    if stream_prefix and target.startswith(stream_prefix):
        rel = urllib.parse.unquote(target[len(stream_prefix):].lstrip("/"))
        return os.path.join(mount_root, rel)
    if target.startswith("http://") or target.startswith("https://"):
        # 通用兜底：截取 /d/115/ 之后的相对路径
        if "/d/115/" in target:
            return os.path.join(mount_root, urllib.parse.unquote(target.split("/d/115/", 1)[1]))
        if "/115/" in target:
            return os.path.join(mount_root, urllib.parse.unquote(target.split("/115/", 1)[1]))
        return target
    if target.startswith('/movies/'):
        return '/Movies/' + target[8:]
    return target


def _match_in_listing(parent: str, name: str) -> Optional[str]:
    """列父目录（同时促使 CD2 刷新该目录缓存），按原名 / Unicode 规范化 / 忽略大小写 / 首尾空格查找实际文件名"""
    try:
        entries = os.listdir(parent)
    except OSError:
        return None
    if name in entries:
        return name
    nfc = unicodedata.normalize("NFC", name)
    for e in entries:
        if unicodedata.normalize("NFC", e) == nfc:
            return e
    low = nfc.casefold()
    for e in entries:
        if unicodedata.normalize("NFC", e).casefold() == low:
            return e
    name_stripped = name.strip()
    low_stripped = low.strip()
    for e in entries:
        if e.strip() == name_stripped or unicodedata.normalize("NFC", e).strip().casefold() == low_stripped:
            return e
    return None


def resolve_cd2_path(host_target: str, diagnostic: Optional[Dict] = None,
                     waits: Tuple[float, ...] = (1.5, 3.0)) -> Optional[str]:
    """确认 CD2 挂载路径真实存在，返回可用路径；不存在返回 None 并在 diagnostic 写明原因。

    CD2 FUSE 对未列过或刚变动的目录可能对单文件 lookup 返回 ENOENT，
    因此未命中时先列父目录促使其刷新，再按递增间隔重试。
    """
    diagnostic = diagnostic if diagnostic is not None else {}
    if os.path.exists(host_target):
        return host_target
    parent, name = os.path.split(host_target)
    for wait in (0.0,) + tuple(waits):
        if wait:
            time.sleep(wait)
        if os.path.isdir(parent):
            actual = _match_in_listing(parent, name)
            if actual:
                path = os.path.join(parent, actual)
                if os.path.exists(path):
                    if actual != name:
                        diagnostic["name_mismatch"] = actual
                    return path
        elif os.path.exists(host_target):
            return host_target

    diagnostic["mapped_path"] = host_target
    # 找到最深的存在的上级目录，区分「挂载不可用 / 目录不存在 / 文件不存在」
    anc = parent
    while anc and not os.path.isdir(anc):
        up = os.path.dirname(anc)
        if up == anc:
            anc = ""
            break
        anc = up
    diagnostic["existing_ancestor"] = anc or "(无)"
    if os.path.isdir(parent):
        diagnostic["reason"] = "目标路径不存在"
        diagnostic["detail"] = "父目录存在但列表中没有该文件（115 上可能已删除或改名，STRM 已过期）"
    else:
        diagnostic["reason"] = "目标路径不存在"
        diagnostic["detail"] = f"父目录不存在，最深可访问目录为 {anc or '(无，CD2 挂载可能不可用)'}（检查 cd2_mount_path 与 STRM 前缀映射，或该目录已在 115 移动）"
    return None


def probe_media_info(video_path: str, timeout_sec: int = 35, diagnostic: Optional[Dict] = None) -> Dict:
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

    last_error = ""
    # 第二次放大探测窗口：高码率 / TS / MKV 在 1MB 内可能还读不到完整视频流参数
    for attempt, (probesize, analyze) in enumerate((("1000000", "1000000"), ("20000000", "10000000"))):
        cmd = [
            'nice', '-n', '15',
            'ionice', '-c', '3',
            'ffprobe', '-v', 'error',
            '-probesize', probesize,
            '-analyzeduration', analyze,
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            video_path
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if diagnostic is not None:
                diagnostic["probe_returncode"] = res.returncode
                diagnostic["probe_stderr"] = (res.stderr or "").strip()[-1000:]
                diagnostic["probe_attempt"] = attempt + 1
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
                try:
                    info["bit_rate"] = int(fmt.get("bit_rate") or 0)
                except Exception:
                    info["bit_rate"] = 0
                # 流概要，探针取不到视频参数时用于诊断（只有音频 / 编码不识别 / 容器异常）
                info["format_name"] = fmt.get("format_name", "")
                info["streams"] = [
                    f"{s.get('codec_type', '?')}:{s.get('codec_name') or s.get('codec_tag_string') or '?'}"
                    + (f" {s.get('width')}x{s.get('height')}" if s.get("width") else "")
                    for s in data.get("streams", [])
                ]

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
                                "aspect": s.get("display_aspect_ratio", ""),
                                "pix_fmt": s.get("pix_fmt", ""),
                                "fps": s.get("avg_frame_rate", ""),
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
            if res.returncode != 0:
                last_error = (res.stderr or "").strip()[-1000:]
        except subprocess.TimeoutExpired as exc:
            last_error = f"TimeoutExpired after {timeout_sec}s"
            if diagnostic is not None:
                diagnostic["probe_timeout"] = True
                diagnostic["probe_attempt"] = attempt + 1
                diagnostic["probe_stderr"] = last_error
            if attempt == 0:
                time.sleep(1)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if diagnostic is not None:
                diagnostic["probe_exception"] = last_error
                diagnostic["probe_attempt"] = attempt + 1
            if attempt == 0:
                time.sleep(1)

    if diagnostic is not None and last_error and not diagnostic.get("probe_stderr"):
        diagnostic["probe_stderr"] = last_error

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


# 8K / 超高码率片源单次抽帧超时（秒）：冷文件需从 115 拉取的数据量与解码耗时都远高于普通片源
HEAVY_TIMEOUT = 120

_decoder_cache: Dict[str, bool] = {}


def has_decoder(name: str) -> bool:
    """ffmpeg 是否编译了指定解码器；检测失败时视为存在，不拦截"""
    if name not in _decoder_cache:
        try:
            res = subprocess.run(['ffmpeg', '-hide_banner', '-decoders'], capture_output=True, text=True, timeout=10)
            names = {parts[1] for parts in (line.split() for line in res.stdout.splitlines()) if len(parts) >= 2}
            _decoder_cache[name] = name in names if res.returncode == 0 and names else True
        except Exception:
            _decoder_cache[name] = True
    return _decoder_cache[name]


def _fps(rate: str) -> float:
    try:
        num, _, den = str(rate).partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def describe_media(media_info: Dict) -> str:
    v = media_info.get("video", {})
    parts = [str(v.get("codec", "?")), f"{v.get('width', '?')}x{v.get('height', '?')}"]
    fps = _fps(v.get("fps", ""))
    if fps:
        parts.append(f"{fps:.0f}fps")
    if v.get("pix_fmt"):
        parts.append(str(v["pix_fmt"]))
    br = media_info.get("bit_rate") or 0
    if br:
        parts.append(f"{br / 1e6:.0f}Mbps")
    size = media_info.get("filesize") or 0
    parts.append(f"{size / 1e6:.1f}MB")
    if not v:
        # 探针没拿到视频参数时，附上容器与流概要
        parts.append(f"容器={media_info.get('format_name') or '?'}")
        parts.append("流=[" + ", ".join(media_info.get("streams") or []) + "]")
    return "片源 " + " ".join(parts)


def is_heavy_media(media_info: Dict) -> bool:
    """8K、4K 高帧率或超高码率片源"""
    v = media_info.get("video", {})
    pixels = int(v.get("width", 0) or 0) * int(v.get("height", 0) or 0)
    uhd = 3840 * 2160
    return (pixels > uhd * 1.2
            or (media_info.get("bit_rate") or 0) > 50_000_000
            or (pixels >= uhd and _fps(v.get("fps", "")) > 50))


_FFMPEG_HINTS = (
    (("Cannot allocate memory", "Out of memory"), "内存不足（8K 解码占用大）"),
    (("hardware accelerated AV1", "codec av1"), "ffmpeg 不支持 AV1 软解，需安装带 libdav1d 的 ffmpeg"),
    (("Decoder (codec",), "ffmpeg 缺少该编码的解码器"),
    (("Input/output error", "Connection", "Operation timed out"), "CD2 读取 I/O 错误"),
    (("Invalid data found", "Error parsing OBU", "Invalid repeated frame header"), "数据无效（文件损坏或 CD2 读取不完整）"),
    (("Could not find ref", "PPS id out of range", "missing picture"), "seek 落在不可独立解码的帧"),
    (("does not contain any stream",), "输入中没有可解码的视频流（只有音频，或视频编码 ffmpeg 不识别）"),
    (("Output file is empty", "nothing was encoded", "未输出画面"), "未解码出任何画面"),
    (("超时",), "读取/解码超时（高码率冷文件需从 115 拉取大量数据）"),
)


def _explain_ffmpeg_error(text: str) -> str:
    hints = [hint for keys, hint in _FFMPEG_HINTS if any(k in text for k in keys)]
    return ("判断：" + "、".join(dict.fromkeys(hints)) + "；") if hints else ""


def _summarize_stderr(stderr: str, max_lines: int = 4) -> str:
    """ffmpeg 错误输出去掉 [xxx @ 0x...] 前缀并去重，保留最后几条不同的信息"""
    lines = []
    for line in (stderr or "").splitlines():
        line = re.sub(r"^\[[^\]]*@ (0x)?[0-9a-fA-F]+\]\s*", "", line.strip())
        if line and line not in lines:
            lines.append(line)
    return " / ".join(lines[-max_lines:])[:300]


NO_VIDEO_MARK = "CD2 探针解析出的流中没有视频流"
# 读取 / 容器层面的错误：可能是 CD2 临时读坏，值得换 HTTP 直链再试一次
# （142 实测 CD2 与直链读到的字节一致，解码层面的错误换直链无意义，不在此列）
_IO_ERRORS = ("Input/output error", "Connection", "Operation timed out", "moov atom not found",
              "Error opening input", "does not contain any stream", NO_VIDEO_MARK)
# 码流本身非法：换位置 / 换通道都解不出来，判定为文件损坏
_BITSTREAM_ERRORS = ("Error parsing OBU", "Invalid repeated frame header", "Error parsing frame header",
                     "zero_bit out of range", "Failed to parse temporal unit")
CORRUPT_MARK = "文件本身很可能已损坏"

# 每次抽帧最多读取的片源时长（秒）：坏文件逐包报错时 ffmpeg 会一直读到文件末尾，
# CD2 冷读仅约 0.7MB/s，不加限制一个 8K 坏文件要从 115 拉取整个文件
GRAB_READ_LIMIT = 15
# 输出 JPEG 每像素字节数低于此值视为黑场 / 近纯色画面（片头淡入常见），换位置重抽
DARK_BYTES_PER_PIXEL = 0.02
# 直链复核前的间隔（秒），与 115 单 Cookie 流控底线一致
HTTP_FALLBACK_DELAY = 2.0


def _grab_frame(src: str, out_path: str, seek: int, max_threads: int, timeout: int,
                key_only: bool = True) -> Tuple[bool, str]:
    """抽一帧写入 out_path，返回 (成功, 错误摘要)"""
    cmd = ['nice', '-n', '15', 'ionice', '-c', '3',
           'ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-y',
           '-threads', str(max_threads)]
    if key_only:
        # 只解码关键帧：输出 seek 落点所在的关键帧，最快；
        # -copyts 防止 seek 点后已无关键帧时落点关键帧被当作早于起点丢弃
        cmd += ['-skip_frame', 'nokey', '-ss', time.strftime('%H:%M:%S', time.gmtime(seek)),
                '-noaccurate_seek', '-copyts']
    else:
        # 全帧：从落点关键帧解码到目标时间点附近，画面更贴近 seek 位置
        cmd += ['-ss', time.strftime('%H:%M:%S', time.gmtime(seek)), '-noaccurate_seek']
    cmd += ['-t', str(GRAB_READ_LIMIT), '-i', src, '-an', '-sn', '-dn',
            '-frames:v', '1',
            '-vf', 'scale=w=min(1080\\,iw):h=-2',
            '-q:v', '3', out_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            return True, ""
        return False, _summarize_stderr(res.stderr) or f"返回码 {res.returncode}，未输出画面"
    except subprocess.TimeoutExpired:
        return False, f"超时 {timeout}s"


def _looks_dark(size: int, video: Dict) -> bool:
    """按输出尺寸估算 JPEG 体积，过小说明是黑场或近纯色画面"""
    w = int(video.get("width") or 0)
    h = int(video.get("height") or 0)
    if w > 0 and h > 0:
        out_w = min(1080, w)
        pixels = out_w * (h * out_w / w)
    else:
        pixels = 1080 * 1080
    return size < pixels * DARK_BYTES_PER_PIXEL


def _remove_quiet(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def extract_cover_by_cd2(
    target_path: str,
    poster_path: str,
    thumb_path: Optional[str] = None,
    seek_sec: int = 0,
    nfo_path: Optional[str] = None,
    max_threads: int = 2,
    timeout_sec: int = 35,
    diagnostic: Optional[Dict] = None,
    cd2_mount_path: Optional[str] = None,
    stream_prefix: Optional[str] = None
) -> bool:
    """走 CD2 本地挂载 Fast Seek 进行高效降权抽帧，顺便提取完整元数据反哺 NFO"""
    diagnostic = diagnostic if diagnostic is not None else {}
    try:
        # 动态将远程 URL 前缀映射为本地 CD2 真实物理挂载路径
        host_target = map_to_cd2_path(target_path, cd2_mount_path, stream_prefix)

        resolved = resolve_cd2_path(host_target, diagnostic)
        if not resolved:
            return False
        host_target = resolved
        diagnostic["mapped_path"] = host_target
        if os.path.isdir(host_target):
            diagnostic["reason"] = "目标实际是目录"
            return False

        # 确保输出目录存在
        out_dir = os.path.dirname(poster_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        # 1. 轻量级快速探针 (顺带读取大小、总时长、视音频流)
        media_diag = {}
        media_info = probe_media_info(host_target, timeout_sec=35, diagnostic=media_diag)
        no_video = False
        if not media_info.get("valid"):
            # 探针失败不放弃：按 60 秒时长兜底继续抽帧；valid=False 保证不会把空流信息写进 NFO
            if media_diag.get("probe_timeout"):
                probe_note = "探针超时"
            elif media_diag.get("probe_returncode", 0) not in (0, None):
                probe_note = "探针解析失败"
            else:
                probe_note = "探针未取得时长或宽高"
            diagnostic.update(media_diag)
            diagnostic["probe_note"] = probe_note
            streams = media_info.get("streams") or []
            if streams and not any(s.startswith("video:") for s in streams):
                # 探针正常解析出了流，但没有视频流：CD2 上再跑 ffmpeg 也不会有画面，只剩 HTTP 直链复核
                no_video = True
            if not media_info.get("duration"):
                media_info["duration"] = 60.0

        # 2. 截取点策略（超长直播/大文件保护）：
        # 对于超长录播 (>10分钟 / 600秒)，坚决不要 seek 到几个小时的中间去拉取数 GB 远端数据，极易触发跨洋超时
        # 统一截取片头 3-15 秒（精彩开场），秒级出图且零流量浪费
        ext = os.path.splitext(host_target)[1].lower()
        real_duration = media_info.get("duration", 0.0)
        
        if seek_sec > 0:
            target_seek = seek_sec
        elif ext == '.webm':
            target_seek = 3
        elif real_duration > 600:
            # 录播长视频：截取第 10 秒
            target_seek = 10
        elif real_duration > 10:
            target_seek = int(real_duration / 2)
        else:
            # 短视频
            target_seek = max(0, min(3, int(real_duration / 2)))

        v = media_info.get("video", {})
        diagnostic["media"] = describe_media(media_info)
        # 探针失败多半是读取慢或高码率，按重型片源放宽超时
        heavy = is_heavy_media(media_info) or not media_info.get("valid")

        # AV1 在 ffmpeg 中只有 libdav1d / libaom 能软解，内置 av1 解码器需要硬件加速
        codec = str(v.get("codec", "")).lower()
        if codec == "av1" and not (has_decoder("libdav1d") or has_decoder("libaom-av1")):
            diagnostic["reason"] = "ffmpeg 不支持 AV1 软解"
            diagnostic["detail"] = "服务器 ffmpeg 未编译 libdav1d，无法解码 AV1（8K 片源常见），请安装带 libdav1d 的 ffmpeg"
            return False

        # 3. 降权抽帧；8K / 超高码率放宽超时
        grab_timeout = max(timeout_sec, HEAVY_TIMEOUT) if heavy else timeout_sec
        # 依次：目标点关键帧（最快）→ 目标点全帧（关键帧是片头黑场时）→ 片头全帧（关键帧标记异常时）
        attempts = [(target_seek, True), (target_seek, False)] + ([(0, False)] if target_seek > 0 else [])
        errors = [f"{NO_VIDEO_MARK}"] if no_video else []
        tries = []          # (路径, 大小)：画面过暗时先留着，全部尝试后取最大的
        chosen = None
        for i, (seek, key_only) in enumerate([] if no_video else attempts):
            out = f"{poster_path}.try{i}.jpg"
            tries.append(out)
            got, err = _grab_frame(host_target, out, seek, max_threads, grab_timeout, key_only)
            label = f"seek={seek}s{'' if key_only else '(全帧)'}"
            if got:
                if not _looks_dark(os.path.getsize(out), v):
                    chosen = out
                    break
                errors.append(f"{label}: 画面过暗/近纯色，换位置重抽")
                continue
            errors.append(f"{label}: {err}")
            if any(k in err for k in _BITSTREAM_ERRORS):
                # 码流非法：只在片头再确认一次，避免对坏文件反复整段读取
                if seek > 0 or key_only:
                    out0 = f"{poster_path}.try_head.jpg"
                    tries.append(out0)
                    got0, err0 = _grab_frame(host_target, out0, 0, max_threads, grab_timeout, False)
                    if got0:
                        chosen = out0
                    else:
                        errors.append(f"seek=0s(全帧): {err0}")
                break
            # 读取超时说明 CD2 拉取过慢，再换位置重试只会叠加等待
            if err.startswith("超时") and sum(1 for e in errors if "超时" in e) >= 2:
                break
        if chosen is None:
            produced = [p for p in tries if os.path.exists(p) and os.path.getsize(p) > 1000]
            if produced:
                # 所有位置都偏暗：取信息量最大的一张，好过没有封面
                chosen = max(produced, key=os.path.getsize)
        joined = " | ".join(errors)
        bitstream_bad = chosen is None and any(k in joined for k in _BITSTREAM_ERRORS)
        if (chosen is None and not bitstream_bad and target_path.startswith(("http://", "https://"))
                and any(k in joined for k in _IO_ERRORS)):
            # 读取 / 容器层面异常：换 STRM 直链读一次，区分「CD2 临时读坏」与「文件本身损坏」
            # 直链会向 115 申请一次下载地址，先按流控底线间隔
            time.sleep(HTTP_FALLBACK_DELAY)
            out = f"{poster_path}.try_http.jpg"
            tries.append(out)
            got, err = _grab_frame(target_path, out, attempts[0][0], max_threads, grab_timeout, False)
            if got:
                chosen = out
                diagnostic["note"] = ("CD2 读到的文件不含视频流，HTTP 直链正常，已改用直链抽帧" if no_video
                                      else "CD2 读取数据异常，已改用 HTTP 直链抽帧成功")
            else:
                errors.append(f"HTTP直链: {err}")
                if any(k in err for k in _IO_ERRORS + ("Invalid data found",) + _BITSTREAM_ERRORS):
                    errors.insert(0, f"CD2 与 HTTP 直链读到的数据都无法解码，{CORRUPT_MARK}或不含视频")
                    diagnostic["permanent"] = True
                joined = " | ".join(errors)
        if bitstream_bad:
            errors.insert(0, f"视频码流非法（CD2 与直链读到的字节一致，已排除读取问题），{CORRUPT_MARK}")
            joined = " | ".join(errors)
            diagnostic["permanent"] = True
        ok = chosen is not None
        if ok:
            os.replace(chosen, poster_path)
        for p in tries:
            _remove_quiet(p)
        if not ok:
            diagnostic["reason"] = "FFmpeg 抽帧超时" if all("超时" in e for e in errors) else "FFmpeg 抽帧失败"
            probe_note = f"{diagnostic['probe_note']}，按 60 秒兜底；" if diagnostic.get("probe_note") else ""
            diagnostic["detail"] = f"{diagnostic['media']}；{probe_note}{_explain_ffmpeg_error(joined)}{joined}"[:1600]
            return False

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
        if not diagnostic.get("reason"):
            diagnostic["reason"] = "未生成有效封面文件"
        return False
    except Exception as exc:
        diagnostic["reason"] = "抽帧异常"
        diagnostic["detail"] = f"{type(exc).__name__}: {str(exc)[:140]}"
        return False
