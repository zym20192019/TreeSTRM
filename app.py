import scraper_service
import os, sys, re, io, time, json, asyncio, threading, requests, subprocess, urllib.parse, shlex
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

BASE_DIR = "/opt/treestrm"
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_FILE = os.path.join(DATA_DIR, "history.json")
SPEED_FILE = os.path.join(DATA_DIR, "speed.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

COMPREHENSIVE_VIDEO_EXTS = {
    '.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v',
    '.ts', '.m2ts', '.mts', '.vob', '.iso', '.rmvb', '.rm', '.asf',
    '.264', '.265', '.hevc', '.mpg', '.mpeg', '.f4v'
}

COMPREHENSIVE_SUBTITLE_EXTS = {
    '.srt', '.ass', '.ssa', '.vtt', '.sub', '.sup', '.idx', '.smi'
}

live_log_messages: List[str] = []
MAX_LIVE_LOGS = 1000

TG_BOT_TOKEN = ""
TG_CHAT_ID = "5662349315"

def send_telegram_alert(text: str):
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        pass

def cleanup_logs_older_than_7_days():
    """自动清理超过 7 天的历史日志文件"""
    try:
        now = time.time()
        cutoff = now - (7 * 86400)
        import glob
        for p in glob.glob(os.path.join(LOG_DIR, "treestrm_*.log")):
            if os.path.getmtime(p) < cutoff:
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass

def push_log(msg: str):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    live_log_messages.append(entry)
    if len(live_log_messages) > MAX_LIVE_LOGS:
        live_log_messages.pop(0)

    # 7 天持久化滚动存储
    today = time.strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"treestrm_{today}.log")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

# 强制红线：单 Cookie 慢牛流控底线 >= 2.0s
MIN_SAFE_DELAY = 2.0

def get_dynamic_delay() -> float:
    try:
        if os.path.exists(SPEED_FILE):
            with open(SPEED_FILE, "r") as f:
                d = json.load(f)
                val = float(d.get("delay", 2.0))
                return max(MIN_SAFE_DELAY, val)
    except Exception:
        pass
    return 2.0

sync_progress = {
    "is_syncing": False,
    "rule_name": "",
    "stage": "就绪",
    "total": 0,
    "processed": 0,
    "created": 0,
    "deleted": 0,
    "skipped": 0,
    "elapsed": 0.0
}

governance_queue: List[dict] = []
current_running_job: Optional[dict] = None

gov_progress = {
    "is_running": False,
    "category": "",
    "stage": "就绪",
    "total_videos": 0,
    "processed_videos": 0,
    "covers_extracted": 0,
    "nfos_generated": 0,
    "ai_batches_done": 0,
    "elapsed": 0.0,
    "queue_length": 0,
    "is_paused_for_captcha": False,
    "recent_success_streak": 0,
    "total_api_calls": 0
}

DEFAULT_PROTECTED_CATEGORIES = ["欧美刮削", "欧美刮削_01", "AV女优", "FC2", "我爱的AV", "无码刮削", "AdultMovie"]

# 115 官方标准伪装指纹头
OFFICIAL_115_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Origin': 'https://115.com',
    'Referer': 'https://115.com/'
}

def load_cookie_from_env() -> str:
    env_p = "/opt/115-agent/.env"
    if os.path.exists(env_p):
        with open(env_p, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("PAN115_COOKIE="):
                    return line.strip().split("=", 1)[1]
    return ""

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if not cfg.get("cookie"):
                    cfg["cookie"] = load_cookie_from_env()
                if "protected_categories" not in cfg:
                    cfg["protected_categories"] = DEFAULT_PROTECTED_CATEGORIES
                if "ai_governance" not in cfg:
                    cfg["ai_governance"] = {
                        "enabled": True,
                        "api_base": "https://openrouter.ai/api/v1",
                        "api_key": "",
                        "model": "minimax/minimax-m2.7:free",
                        "batch_size": 100
                    }
                return cfg
        except Exception:
            pass
    cfg = {
        "cookie": load_cookie_from_env(),
        "default_prefix": "/movies/CloudDrive/115",
        "default_output": "/Movies/TreeStrms",
        "layer_limit": 25,
        "sync_subtitles": True,
        "auto_cleanup_orphan": True,
        "cover_threads": 3,
        "protected_categories": DEFAULT_PROTECTED_CATEGORIES,
        "ai_governance": {
            "enabled": True,
            "api_base": "https://openrouter.ai/api/v1",
            "api_key": "",
            "model": "minimax/minimax-m2.7:free",
            "batch_size": 100
        },
        "auto_sync_enabled": False,
        "auto_sync_interval_mins": 30,
        "rules": [
            {
                "id": "chengren_all",
                "name": "成人",
                "cid": "3291659674416491425",
                "output_dir": "/Movies/TreeStrms",
                "prefix": "/movies/CloudDrive/115",
                "sync_subtitles": True,
                "auto_cleanup_orphan": True,
                "enabled": True,
                "last_sync": None,
                "last_status": "就绪",
                "video_count": 0,
                "sub_count": 0,
                "ext_stats": {}
            }
        ]
    }
    save_config(cfg)
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def load_history() -> list:
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def append_history(entry: dict):
    hist = load_history()
    hist.insert(0, entry)
    hist = hist[:50]
    with open(LOGS_FILE, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

def create_115_session(cookie_str: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(OFFICIAL_115_HEADERS)
    for item in str(cookie_str or "").split(';'):
        if '=' in item:
            k, v = item.strip().split('=', 1)
            s.cookies.set(k.strip(), v.strip(), domain='.115.com')
            s.cookies.set(k.strip(), v.strip())
    return s

def export_115_tree(cookie: str, cid: str = "0", layer_limit: int = 25) -> str:
    push_log(f"向 115 发起目录树导出 (CID: {cid}, 最深: {layer_limit}层)...")
    session = create_115_session(cookie)
    
    data = {
        'file_ids': str(cid),
        'target': f'U_1_{cid}',
        'layer_limit': str(layer_limit)
    }
    res = session.post('https://webapi.115.com/files/export_dir', data=data, timeout=20).json()
    export_id = res.get('data', {}).get('export_id') if isinstance(res.get('data'), dict) else None
    
    pick_code = None
    push_log("正在轮询等待 115 服务端生成大树文件...")
    
    for i in range(60):
        time.sleep(2)
        url = 'https://webapi.115.com/files/export_dir' + (f'?export_id={export_id}' if export_id else '')
        st_res = session.get(url, timeout=15).json()
        raw_d = st_res.get('data', {})
        if isinstance(raw_d, dict) and raw_d.get('pick_code'):
            pick_code = raw_d['pick_code']
            fn = raw_d.get('file_name', '目录树.txt')
            push_log(f"✅ 115 目录树快照已就绪: {fn} (pickcode: {pick_code})")
            break
        if i > 0 and i % 5 == 0:
            push_log(f"等待 115 服务端处理中... 已耗时 {i*2} 秒")
            
    if not pick_code:
        raise TimeoutError("等待 115 目录树生成超时")
        
    dl_res = session.get(f'https://webapi.115.com/files/download?pickcode={pick_code}', timeout=15).json()
    dl_url = dl_res.get('file_url') or (dl_res.get('data') or {}).get('file_url')
    if not dl_url:
        raise RuntimeError("获取目录树下载直链失败")
        
    push_log(f"正在携带认证 Cookie 从 115 CDN 拉取目录树...")
    resp = session.get(dl_url, timeout=90)
    
    if resp.status_code != 200 or len(resp.content) < 500:
        raise RuntimeError(f"目录树下载失败 (HTTP {resp.status_code}, 长度: {len(resp.content)} bytes)")
        
    mb_size = round(len(resp.content) / (1024 * 1024), 2)
    push_log(f"目录树全量下载成功！大小: {mb_size} MB")
    return resp.content.decode('utf-16le', errors='ignore')

def parse_tree_1to1(content: str, target_dir_name: str = "成人", include_subs: bool = True) -> tuple:
    push_log("正在合并软换行并解析目录树结构...")
    clean_lines = []
    current_line = ''
    for raw_line in io.StringIO(str(content or "")):
        line = str(raw_line or "").replace("\ufeff", "").rstrip("\r\n")
        if not line.strip(): continue
        if line.startswith('|') or line.startswith('﻿|'):
            if current_line: clean_lines.append(current_line)
            current_line = line
        else:
            current_line += ' ' + line.strip()
    if current_line: clean_lines.append(current_line)

    path_stack = {}
    matched_videos = []
    matched_subs = []
    ext_stats = {}
    
    for line in clean_lines:
        level = line.count("|")
        clean_name = re.sub(r"^[|\s—\-]+", "", line).strip()
        if not clean_name: continue
        
        for stale_level in [k for k in path_stack.keys() if k > level]:
            path_stack.pop(stale_level, None)
        path_stack[level] = clean_name
        
        parts = [path_stack[d] for d in range(level + 1) if d in path_stack]
        if parts and parts[0] in ['根目录', 'ROOT', '']:
            parts = parts[1:]
        if not parts: continue
        
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

# ---------- 智能文字语义与 AI 标签治理核心 ----------

def extract_rule_tags(rel_path: str) -> dict:
    parts = rel_path.strip('/').split('/')
    filename = parts[-1]
    base, _ = os.path.splitext(filename)
    
    genres = []
    tags = []
    studio = ""
    
    if len(parts) >= 2:
        top_category = parts[1] if len(parts) > 1 and parts[0] == "成人" else parts[0]
        genres.append(top_category)
        tags.append(top_category)
        
    if len(parts) >= 3:
        sub_folder = parts[2] if parts[0] == "成人" else parts[1]
        studio = sub_folder
        tags.append(sub_folder)
        if "舞" in sub_folder:
            tags.extend(["舞蹈", "舞团"])
        elif "街拍" in sub_folder or "户外" in sub_folder:
            tags.extend(["街拍", "户外"])
        elif "车模" in sub_folder:
            tags.extend(["车模", "车展"])
        elif "动漫" in sub_folder or "3d" in sub_folder.lower():
            tags.extend(["3D动漫", "二次元", "同人"])
        elif "直播" in sub_folder or "BJ" in sub_folder:
            tags.extend(["主播", "直播录屏"])
            
    if re.search(r'4k|2160p', filename, re.IGNORECASE):
        tags.append("4K原画")
    if re.search(r'竖|vt|vertical', filename, re.IGNORECASE):
        tags.append("竖屏")
    if re.search(r'横|hz|horizontal', filename, re.IGNORECASE):
        tags.append("横屏")
    if re.search(r'MMD', filename, re.IGNORECASE):
        tags.append("MMD")
    if re.search(r'VAM', filename, re.IGNORECASE):
        tags.append("VAM")
        
    unique_tags = list(dict.fromkeys(tags))
    unique_genres = list(dict.fromkeys(genres))
    
    return {
        "title": base,
        "studio": studio,
        "genres": unique_genres or ["精选"],
        "tags": unique_tags
    }

def call_ai_auditor(folder_name: str, file_names: List[str], ai_cfg: dict) -> Optional[dict]:
    if not ai_cfg.get("enabled") or not ai_cfg.get("api_key"):
        return None
        
    api_base = (ai_cfg.get("api_base") or "https://openrouter.ai/api/v1").rstrip("/")
    api_key = ai_cfg.get("api_key")
    model = ai_cfg.get("model") or "minimax/minimax-m2.7:free"
    
    sample_files = file_names[:60]
    prompt = f"""你是一个媒体库元数据与标签治理专家。
请根据以下视频专区名称及文件列表，进行标签清洗、去重、归一化和题材提炼。

【专区目录】: {folder_name}
【部分文件列表】:
""" + "\n".join([f"- {fn}" for fn in sample_files]) + """

请严格输出合法的 JSON 格式（不要输出 markdown 代码块标记，直接以大括号开头结尾）：
{
  "studio": "规范制作者/舞团/工作室名",
  "genre": "核心分类(如: 舞蹈/街拍/直播/3D动漫)",
  "common_tags": ["该专区全部视频通用的规范标签列表, 如: 舞团, 热舞, 4K原画, 舞台表演"],
  "clean_summary": "简短的一句话专区看点介绍(30字以内)"
}
"""
    try:
        url = f"{api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/treestrm",
            "X-Title": "TreeSTRM",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个严谨的媒体库元数据治理专家，只输出合法 JSON。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2
        }
        resp = requests.post(url, headers=headers, json=data, timeout=25).json()
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        # 匹配大括号提取合法 JSON (支持 reasoning 模型)
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match and resp.get("choices", [{}])[0].get("message", {}).get("reasoning"):
            # 有些模型只输出在 reasoning 里或者被包裹
            pass
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        push_log(f"⚠️ AI 标签审批批次跳过 (降级走规则): {str(e)[:60]}")
    return None

def call_ai_item_auditor(file_names: List[str], folder_hint: str, ai_cfg: dict) -> Dict[str, dict]:
    """
    针对单批文件（建议 30-50 个）一次性送入大模型，
    依据各自文件名中的特色词（如模特、服饰、剧情、编号）精准提取每个视频独有的标签和清洗标题。
    返回: { "文件名/base_fn": {"clean_title": "...", "tags": [...], "summary": "..."} }
    """
    if not ai_cfg.get("enabled") or not ai_cfg.get("api_key") or not file_names:
        return {}
        
    api_base = (ai_cfg.get("api_base") or "https://openrouter.ai/api/v1").rstrip("/")
    api_key = ai_cfg.get("api_key")
    model = ai_cfg.get("model") or "gemini-3.1-flash-lite"
    
    prompt = f"""你是媒体库元数据与标签治理专家。
专区环境: {folder_hint}

请仔细分析以下视频文件名列表。很多文件名中包含了重要的人名/模特、服饰(如瑜伽裤/皮裤/旗袍)、场景或编号等信息。
请为每个文件提取/清洗出：
1. clean_title: 去除编号、格式后缀、无用哈希字符(#)后的优雅展示标题
2. tags: 从该文件名中精准提炼出的专属于该视频的特征标签列表（如服饰、模特、动作、拍摄类型等，3-6个）
3. summary: 结合文件名看点生成的简短简介（25字以内）

输出必须严格为合法的 JSON 对象（严禁包含 markdown 代码块包裹标记，直接以大括号 {{ 开头结尾），格式如下：
{{
  "原文件名": {{
    "clean_title": "清洗后的展示标题",
    "tags": ["标签1", "标签2"],
    "summary": "简述"
  }}
}}

待处理文件名列表：
""" + "\n".join([f"- {fn}" for fn in file_names])

    try:
        url = f"{api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/treestrm",
            "X-Title": "TreeSTRM",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个严谨的媒体库元数据治理专家，必须严格输出合法JSON对象。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2
        }
        resp = requests.post(url, headers=headers, json=data, timeout=30).json()
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, dict):
                return parsed
    except Exception as e:
        push_log(f"⚠️ 批量文件名 AI 提炼跳过: {str(e)[:60]}")
    return {}

def write_nfo_file(nfo_path: str, meta: dict, runtime_mins: int = 0, filesize: int = 0, width: int = 1920, height: int = 1080):
    tags_xml = "\n".join([f"  <tag>{t}</tag>" for t in meta.get("tags", [])])
    genres_xml = "\n".join([f"  <genre>{g}</genre>" for g in meta.get("genres", [])])
    studio = meta.get("studio", "")
    title = meta.get("title", "")
    summary = meta.get("summary", "")
    
    filesize_tag = f"  <filesize>{filesize}</filesize>\n" if filesize > 0 else ""
    runtime_tag = f"  <runtime>{runtime_mins}</runtime>\n" if runtime_mins > 0 else ""
    
    content = f"""<?xml version="1.0" encoding="UTF-8" ?>
<movie>
  <title><![CDATA[{title}]]></title>
  <originaltitle><![CDATA[{title}]]></originaltitle>
  <sorttitle><![CDATA[{title}]]></sorttitle>
  <studio>{studio}</studio>
  <plot><![CDATA[{summary}]]></plot>
  <outline><![CDATA[{summary}]]></outline>
{genres_xml}
{tags_xml}
{runtime_tag}{filesize_tag}  <fileinfo>
    <streamdetails>
      <video>
        <width>{width}</width>
        <height>{height}</height>
      </video>
    </streamdetails>
  </fileinfo>
</movie>"""
    with open(nfo_path, "w", encoding="utf-8") as f:
        f.write(content)

# ----------------- CD2 本地通道 Fast Seek 抽帧核心 (0 验证码、0 拦截、0.9s 秒级出图) -----------------

from engine.cd2_extractor import extract_cover_by_cd2, probe_media_info, update_nfo_with_streamdetails


def has_streamdetails_in_nfo(nfo_path: str) -> bool:
    """严格判定 NFO 是否已同时含有有效的视频流与音频流编码信息"""
    if not nfo_path or not os.path.exists(nfo_path):
        return False
    try:
        with open(nfo_path, "r", encoding="utf-8", errors="ignore") as f:
            chunk = f.read(8192)
            has_video = "<video>" in chunk and "<codec>" in chunk
            has_audio = "<audio>" in chunk and "<codec>" in chunk
            return has_video and has_audio
    except Exception:
        return False


# ----------------- 全量目录树 STRM 极速生成 (独立模块) -----------------

def sync_strms_pure_1to1(
    video_paths: List[str],
    sub_paths: List[str],
    output_dir: str, 
    strm_prefix: str,
    auto_cleanup: bool = True
) -> tuple:
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    strm_prefix = str(strm_prefix or "").rstrip('/')
    
    push_log("正在构建本次云端目标 STRM 完整集合...")
    expected_strms = {}
    
    # 严格只有正片视频生成 .strm 播放索引，字幕文件绝不生成/覆盖 .strm
    for rel_path in video_paths:
        dirname = os.path.dirname(rel_path)
        filename = os.path.basename(rel_path)
        basename, raw_ext = os.path.splitext(filename)
        
        # 安全防御拦截：如果扩展名属于字幕或非视频，坚决丢弃
        if raw_ext.lower() in COMPREHENSIVE_SUBTITLE_EXTS:
            continue
            
        strm_filename = f"{basename}.strm"
        target_path = os.path.join(output_dir, dirname, strm_filename) if dirname else os.path.join(output_dir, strm_filename)
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
                except Exception: pass
            push_log(f"已成功清理 {v_deleted} 个过期无效 STRM 索引文件（元数据海报保持完整）！")

    total_to_process = len(expected_strms)
    sync_progress["total"] = total_to_process
    sync_progress["processed"] = 0
    
    push_log("开始增量写入 STRM 播放索引...")
    batch_count = 0
    
    for target_path, content in expected_strms.items():
        batch_count += 1
        sync_progress["processed"] = batch_count
        
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        
        if target_path not in existing_local_strms:
            try:
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(content)
                v_created += 1
            except Exception: pass
        else:
            v_skipped += 1

        if batch_count % 30000 == 0 or batch_count == total_to_process:
            pct = round(batch_count / total_to_process * 100, 1)
            push_log(f"STRM 索引生成进度: {batch_count}/{total_to_process} ({pct}%) - 已新建: {v_created}, 跳过已有: {v_skipped}")

    push_log(f"🎉 全量 STRM 生成完成！新增: {v_created}，跳过已有: {v_skipped}，清理过期: {v_deleted}")
    return v_created, v_skipped, v_deleted

# ----------------- 二级页面：父子直属结构 100% 精准 CID 映射引擎 -----------------

category_sub_cids_cache: Dict[str, Dict[str, str]] = {}

def get_category_cid_map(session: requests.Session, category_name: str) -> Dict[str, str]:
    if category_name in category_sub_cids_cache:
        return category_sub_cids_cache[category_name]
        
    cid_map = {}
    try:
        adult_cid = "3291659674416491425"
        res = session.get(f'https://webapi.115.com/files?aid=1&cid={adult_cid}&show_dir=1&limit=100', timeout=12).json()
        cat_cid = None
        for d in res.get('data', []):
            if d.get('n') == category_name:
                cat_cid = d.get('cid')
                break
                
        if cat_cid:
            sub_res = session.get(f'https://webapi.115.com/files?aid=1&cid={cat_cid}&show_dir=1&limit=200', timeout=12).json()
            for sd in sub_res.get('data', []):
                s_name = sd.get('n')
                s_cid = sd.get('cid')
                if s_name and s_cid:
                    cid_map[s_name] = s_cid
            category_sub_cids_cache[category_name] = cid_map
            push_log(f"⚡ [直属目录树对齐] 成功锁定【{category_name}】直属 {len(cid_map)} 个子专区真实 CID 索引表！")
    except Exception as e:
        push_log(f"⚠️ 拉取直属子目录树异常: {str(e)[:50]}")
    return cid_map

def fetch_folder_pickcodes(session: requests.Session, category_name: str, parent_path: str) -> Dict[str, dict]:
    folder_name = os.path.basename(parent_path)
    res_map = {}
    try:
        cid_map = get_category_cid_map(session, category_name)
        target_cid = cid_map.get(folder_name)
        
        if not target_cid:
            s_res = session.get(f'https://webapi.115.com/files/search?search_value={urllib.parse.quote(folder_name)}&limit=5', timeout=10).json()
            raw_data = s_res.get('data')
            if isinstance(raw_data, list) and len(raw_data) > 0:
                for d in raw_data:
                    if isinstance(d, dict) and d.get('n') == folder_name and d.get('cid'):
                        target_cid = d.get('cid')
                        break
                if not target_cid and isinstance(raw_data[0], dict):
                    target_cid = raw_data[0].get('cid')
                    
        if target_cid:
            files_res = session.get(f'https://webapi.115.com/files?aid=1&cid={target_cid}&show_dir=0&limit=1150', timeout=12).json()
            raw_files = files_res.get('data')
            if isinstance(raw_files, list):
                for f in raw_files:
                    if isinstance(f, dict) and f.get('n') and f.get('pc'):
                        res_map[f.get('n')] = {
                            'pc': f.get('pc'),
                            'size': int(f.get('s') or 0),
                            'play_long': int(f.get('play_long') or 0),
                            'vdi': f.get('vdi')
                        }
    except Exception as e:
        push_log(f"⚠️ 获取目录【{folder_name}】内部映射异常: {str(e)[:50]}")
    return res_map

# ----------------- 自动队列调度器 -----------------

gov_lock = threading.Lock()

def process_single_category_job(job: dict):
    global current_running_job
    category_name = job.get("category")
    extract_covers = job.get("extract_covers", True)
    probe_streams = job.get("probe_streams", False)
    max_items = job.get("max_items", 0)
    
    current_running_job = job
    gov_progress["is_running"] = True
    gov_progress["category"] = category_name
    gov_progress["stage"] = f"正在查漏补缺【{category_name}】"
    gov_progress["queue_length"] = len(governance_queue)
    gov_progress["is_paused_for_captcha"] = False
    start_t = time.time()
    
    current_delay = get_dynamic_delay()
    push_log(f"======== 🚀 启动【{category_name}】CD2 高速无风控治理流水线 (流控: {current_delay}s, 补流信息: {probe_streams}) ========")
    cfg = load_config()
    ai_cfg = cfg.get("ai_governance", {})
    cookie = cfg.get("cookie", "")
    session = create_115_session(cookie) if cookie else None
    
    base_cat_dir = os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人", category_name)
    if not os.path.exists(base_cat_dir):
        push_log(f"❌ 错误：专区路径不存在: {base_cat_dir}")
        current_running_job = None
        gov_progress["is_running"] = False
        return
        
    sub_batches: Dict[str, List[dict]] = {}
    total_v = 0
    missing_covers_count = 0
    missing_nfos_count = 0
    missing_streams_count = 0
    
    for root, dirs, files in os.walk(base_cat_dir):
        strms = [f for f in files if f.endswith('.strm')]
        if strms:
            folder_items = []
            for sf in strms:
                base_fn = sf[:-5]
                m = re.search(r'\(([^)]+)\)$', base_fn)
                raw_ext = ('.' + m.group(1).lower()) if m else '.mp4'
                ext = m.group(1) if m else 'mp4'
                is_sub = raw_ext in COMPREHENSIVE_SUBTITLE_EXTS
                
                poster = os.path.join(root, f"{base_fn}-poster.jpg")
                nfo = os.path.join(root, f"{base_fn}.nfo")
                has_p = os.path.exists(poster) and os.path.getsize(poster) > 1000
                has_n = os.path.exists(nfo)
                has_s = has_streamdetails_in_nfo(nfo) if has_n else False
                
                # 字幕文件不计入待补封面和待补 NFO 统计
                if not is_sub:
                    if not has_p: missing_covers_count += 1
                    if not has_n: missing_nfos_count += 1
                    if not has_s: missing_streams_count += 1
                
                folder_items.append({
                    "strm_path": os.path.join(root, sf),
                    "base_name": base_fn,
                    "ext": ext,
                    "is_sub": is_sub,
                    "poster_path": poster,
                    "thumb_path": os.path.join(root, f"{base_fn}-thumb.jpg"),
                    "nfo_path": nfo,
                    "has_poster": has_p,
                    "has_nfo": has_n,
                    "has_streamdetails": has_s
                })
            sub_batches[root] = folder_items
            total_v += len(folder_items)

    # 精准统计真正需要工作的条目数 (严格排除字幕文件)
    need_work_items = []
    for root, items in sub_batches.items():
        for it in items:
            if not it.get("is_sub", False):
                needs_work = (not it["has_nfo"]) or (extract_covers and not it["has_poster"]) or (probe_streams and not it["has_streamdetails"])
                if needs_work:
                    need_work_items.append((root, it))
                
    target_limit = len(need_work_items) if (max_items <= 0) else min(len(need_work_items), max_items)
    gov_progress["total_videos"] = target_limit
    gov_progress["processed_videos"] = 0
    gov_progress["covers_extracted"] = 0
    gov_progress["nfos_generated"] = 0
    gov_progress["ai_batches_done"] = 0
    
    push_log(f"专区【{category_name}】全盘盘点完毕：共 {total_v} 部视频，待处理目标: {len(need_work_items)} 部 (待补封面: {missing_covers_count}, 待补 NFO: {missing_nfos_count}, 缺流信息: {missing_streams_count})")
    
    processed_count = 0
    
    for folder_path, items in sub_batches.items():
        if processed_count >= target_limit: break
        if gov_progress["is_paused_for_captcha"]: break
        
        sub_need_work = [it for it in items if ((not it["has_nfo"]) or (extract_covers and not it["has_poster"]) or (probe_streams and not it["has_streamdetails"]))]
        if not sub_need_work:
            continue
            
        sub_folder_name = os.path.basename(folder_path) or category_name
        sample_files = [x["base_name"] for x in items]
        
        # 1. 智能 AI 审批
        needs_nfo = any(not it["has_nfo"] for it in items)
        ai_res = None
        current_ai_cfg = load_config().get("ai_governance", ai_cfg)
        if needs_nfo and current_ai_cfg.get("enabled") and current_ai_cfg.get("api_key"):
            ai_res = call_ai_auditor(f"{category_name} - {sub_folder_name}", sample_files, current_ai_cfg)
            if ai_res:
                gov_progress["ai_batches_done"] += 1
                push_log(f"🤖 [{sub_folder_name}] AI 审批就绪: 分类={ai_res.get('genre')}")

        # 过滤本目录下真正需要处理的条目
        active_items = [it for it in items if ((not it["has_nfo"]) or (extract_covers and not it["has_poster"]) or (probe_streams and not it["has_streamdetails"]))]
        if not active_items:
            continue

        # 2. 针对需要写 NFO 的条目，按批次（每批 30 个）让大模型精准提取文件名特异性元数据
        items_needing_nfo = [it for it in active_items if not it["has_nfo"]]
        file_ai_details = {}
        if items_needing_nfo and current_ai_cfg.get("enabled") and current_ai_cfg.get("api_key"):
            ai_batch_chunk = 30
            for i in range(0, len(items_needing_nfo), ai_batch_chunk):
                batch_slice = items_needing_nfo[i:i + ai_batch_chunk]
                # 将 相对路径（含各级子目录分类上下文）+ 文件名 一并提供给大模型
                batch_items_payload = []
                for x in batch_slice:
                    rel_to_cat = os.path.relpath(x["strm_path"], os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人"))
                    batch_items_payload.append(f"{x['base_name']} (路径层级: {rel_to_cat})")
                names_to_audit = [x["base_name"] for x in batch_slice]
                push_log(f"🧠 [{sub_folder_name}] 正在并发请求 AI 深度提炼文件名与路径关键词 ({i+1}~{min(i+ai_batch_chunk, len(items_needing_nfo))}/{len(items_needing_nfo)})...")
                chunk_res = call_ai_item_auditor(batch_items_payload, f"{category_name} - {sub_folder_name}", current_ai_cfg)
                if chunk_res:
                    # 兼容返回 key 为 base_name 或者带路径的原始 payload
                    for k, v in chunk_res.items():
                        clean_k = k.split(" (路径层级:")[0].strip()
                        file_ai_details[clean_k] = v
                        file_ai_details[k] = v

        # 先顺序极速完成 NFO 补齐与元数据准备
        work_tasks = []
        for it in active_items:
            if processed_count >= target_limit or gov_progress["is_paused_for_captcha"]:
                break
            processed_count += 1
            gov_progress["processed_videos"] = processed_count

            with open(it["strm_path"], "r", encoding="utf-8") as sf:
                raw_target = sf.read().strip()
                video_name = os.path.basename(raw_target)

            # 1. 极速纯文本生成 NFO (不阻塞 CD2 IO，基础骨架秒出)
            if not it["has_nfo"]:
                rel_p = os.path.relpath(it["strm_path"], cfg.get("default_output"))
                meta = extract_rule_tags(rel_p)
                
                # 优先注入大模型针对该单文件的个性化提炼结果
                item_ai = file_ai_details.get(it["base_name"]) or file_ai_details.get(video_name)
                if item_ai and isinstance(item_ai, dict):
                    if item_ai.get("clean_title"):
                        meta["title"] = item_ai.get("clean_title")
                    if item_ai.get("tags") and isinstance(item_ai.get("tags"), list):
                        meta["tags"] = list(dict.fromkeys(meta["tags"] + item_ai.get("tags")))
                    if item_ai.get("summary"):
                        meta["summary"] = item_ai.get("summary")
                
                # 叠加目录级公共元数据
                if ai_res:
                    if ai_res.get("studio") and not meta.get("studio"): meta["studio"] = ai_res.get("studio")
                    if ai_res.get("genre"):
                        g = ai_res.get("genre")
                        meta["genres"] = g if isinstance(g, list) else [g]
                    if ai_res.get("common_tags"):
                        meta["tags"] = list(dict.fromkeys(meta["tags"] + ai_res.get("common_tags")))
                    if ai_res.get("clean_summary") and not meta.get("summary"):
                        meta["summary"] = ai_res.get("clean_summary")
                
                write_nfo_file(it["nfo_path"], meta)
                gov_progress["nfos_generated"] += 1
                it["has_nfo"] = True
                studio_tag = f"[{meta.get('studio')}]" if meta.get('studio') else ""
                genre_tag = f"/{meta.get('genres', [''])[0]}" if meta.get('genres') else ""
                push_log(f"📝 [生成NFO] 【{video_name[:26]}】元数据就绪 {studio_tag}{genre_tag} (封面:{'已存在' if it['has_poster'] else '待抽取'}, 累计NFO: {gov_progress['nfos_generated']})")
            elif probe_streams and not has_streamdetails_in_nfo(it["nfo_path"]):
                push_log(f"📋 [核验NFO] 【{video_name[:26]}】发现历史NFO存在但缺少音视频流，已加入深度补流队列！")

            # 2. 判定是否需要进入 CD2 探针 / 抽封面队列
            needs_poster = extract_covers and not it["has_poster"]
            needs_probe = probe_streams and not has_streamdetails_in_nfo(it["nfo_path"])
            if needs_poster or needs_probe:
                reason = "缺封面+缺流信息" if (needs_poster and needs_probe) else ("缺封面" if needs_poster else "有封面但缺流信息")
                work_tasks.append((raw_target, it["poster_path"], it["thumb_path"], it["nfo_path"], video_name, needs_poster, reason))

        # 3. CD2 本地通道多线程并发 Fast Seek 抽封面 + 顺带探针流信息
        if work_tasks:
            worker_threads = max(1, min(10, int(cfg.get("cover_threads", 3))))
            def _extract_worker(task):
                raw_t, p_path, th_path, nfo_p, v_name, do_poster, reason = task
                try:
                    if do_poster:
                        ok = extract_cover_by_cd2(raw_t, p_path, th_path, nfo_path=nfo_p)
                        if ok:
                            with gov_lock:
                                gov_progress["covers_extracted"] += 1
                                gov_progress["recent_success_streak"] += 1
                                push_log(f"📸 [补齐封面] 【{v_name[:26]}】({reason}) ➔ 成功提取居中黄金封面并注入4K/AAC/大小！(已补封面: {gov_progress['covers_extracted']} 张)")
                    else:
                        # 封面已有，仅需深度探针补充音视频流信息与大小
                        host_target = raw_t
                        if host_target.startswith('/movies/'):
                            host_target = '/Movies/' + host_target[8:]
                        m_info = probe_media_info(host_target, timeout_sec=18)
                        if update_nfo_with_streamdetails(nfo_p, m_info):
                            with gov_lock:
                                gov_progress["recent_success_streak"] += 1
                                v_str = f"{m_info.get('video', {}).get('width')}x{m_info.get('video', {}).get('height')}"
                                a_str = f"{m_info.get('audio', {}).get('codec', 'audio')} {m_info.get('audio', {}).get('channels', 2)}ch"
                                size_mb = round(m_info.get('filesize', 0) / (1024 * 1024), 1)
                                push_log(f"🔍 [深度补流] 【{v_name[:24]}】➔ 成功补全流信息与大小: {v_str}, {a_str}, 大小:{size_mb}MB, 时长:{round(m_info.get('duration', 0))}s")
                        else:
                            push_log(f"⚠️ [探针重试中] 【{v_name[:24]}】CD2响应延迟或超时，保留原NFO绝不污染，留待后续复读")
                except Exception:
                    pass

            with ThreadPoolExecutor(max_workers=worker_threads) as pool:
                list(pool.map(_extract_worker, work_tasks))
                    
    gov_progress["elapsed"] = round(time.time() - start_t, 2)
    done_msg = f"🎉 专区【{category_name}】CD2 无风控治理圆满完成！共补齐 NFO: {gov_progress['nfos_generated']} 个，补齐封面: {gov_progress['covers_extracted']} 张！耗时: {gov_progress['elapsed']}秒"
    push_log(done_msg)
    send_telegram_alert(f"✅ [TreeSTRM 完成通知]\n{done_msg}")
    
    # 动态更新全量底账缓存中该分类的 nfo_count 和 cover_count，保证 UI 刷新立即可见
    try:
        cache_file = os.path.join(DATA_DIR, "categories_cache.json")
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as cf:
                c_map = json.load(cf)
            if category_name in c_map:
                c_map[category_name]["nfo_count"] = c_map[category_name].get("nfo_count", 0) + gov_progress.get("nfos_generated", 0)
                c_map[category_name]["cover_count"] = c_map[category_name].get("cover_count", 0) + gov_progress.get("covers_extracted", 0)
                with open(cache_file, "w", encoding="utf-8") as cf:
                    json.dump(c_map, cf, ensure_ascii=False, indent=2)
    except Exception:
        pass

    try:
        from services.emby_client import EmbyClient
        EmbyClient().trigger_category_refresh(category_name)
    except Exception:
        pass
    current_running_job = None

def queue_worker_loop():
    while True:
        try:
            if governance_queue and not current_running_job and not gov_progress["is_paused_for_captcha"]:
                with gov_lock:
                    next_job = governance_queue.pop(0)
                    gov_progress["queue_length"] = len(governance_queue)
                # 永久免打扰专区拦截（如：有声小说内嵌ID3标签绝不触碰）
                if next_job.get("category") in ["有声小说"]:
                    push_log(f"🛡️ 检测到专区【{next_job.get('category')}】属于免打扰专区，自动跳过治理！")
                    continue
                process_single_category_job(next_job)
            else:
                if not current_running_job and not gov_progress["is_paused_for_captcha"]:
                    gov_progress["is_running"] = False
                    gov_progress["stage"] = "就绪"
                    gov_progress["category"] = ""
                time.sleep(1)
        except Exception as e:
            push_log(f"⚠️ 队列调度异常: {str(e)[:50]}")
            time.sleep(2)

threading.Thread(target=queue_worker_loop, daemon=True).start()

# ----------------- 单独清理文件夹内失效元数据核心 -----------------

from engine.cleaner import clean_orphan_metadata_for_category


# ----------------- FastAPI Web 服务 -----------------

app = FastAPI(title="TreeSTRM 目录树监控控制台", version="2.2.0")

class RuleModel(BaseModel):
    name: str
    cid: str
    output_dir: str
    prefix: str
    sync_subtitles: bool = True
    auto_cleanup_orphan: bool = True
    enabled: bool = True

class AIGovModel(BaseModel):
    enabled: bool = True
    api_base: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    model: str = "minimax/minimax-m2.7:free"
    batch_size: int = 100

class ConfigModel(BaseModel):
    cookie: str
    default_prefix: str
    default_output: str
    layer_limit: int = 25
    sync_subtitles: bool = True
    auto_cleanup_orphan: bool = True
    cover_threads: int = 3
    protected_categories: List[str] = DEFAULT_PROTECTED_CATEGORIES
    ai_governance: AIGovModel
    auto_sync_enabled: bool
    auto_sync_interval_mins: int
    rules: List[RuleModel] = []

sync_lock = threading.Lock()

def do_sync_task(rule_ids: Optional[List[str]] = None):
    with sync_lock:
        sync_progress["is_syncing"] = True
        push_log("======== 开始执行目录树 STRM 全量对账同步 ========")
        cfg = load_config()
        cookie = cfg.get("cookie")
        if not cookie:
            push_log("❌ 错误：115 Cookie 为空，请先在下方保存 Cookie！")
            sync_progress["is_syncing"] = False
            return
            
        rules = cfg.get("rules", [])
        layer_limit = int(cfg.get("layer_limit") or 25)
        
        for r in rules:
            if not r.get("enabled", True): continue
            if rule_ids and r.get("id") not in rule_ids: continue
            
            start_t = time.time()
            r_name = r.get("name")
            cid = r.get("cid", "0")
            out_dir = r.get("output_dir") or cfg.get("default_output")
            prefix = r.get("prefix") or cfg.get("default_prefix")
            sync_subs = r.get("sync_subtitles", True)
            auto_clean = r.get("auto_cleanup_orphan", True)
            
            sync_progress["rule_name"] = r_name
            sync_progress["stage"] = f"正在同步【{r_name}】"
            push_log(f"▶ 正在处理规则【{r_name}】(CID: {cid}) ➔ 落地: {out_dir}")
            
            try:
                tree_text = export_115_tree(cookie, cid=cid, layer_limit=layer_limit)
                videos, subs, ext_stats = parse_tree_1to1(tree_text, target_dir_name=r_name, include_subs=sync_subs)
                vc, vs, vd = sync_strms_pure_1to1(
                    videos, subs, out_dir, prefix, 
                    auto_cleanup=auto_clean
                )
                elapsed = round(time.time() - start_t, 2)
                
                r["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
                r["last_status"] = f"成功 (新增 +{vc}, 跳过 {vs}, 清理过期 -{vd})"
                r["video_count"] = len(videos)
                r["sub_count"] = len(subs)
                r["ext_stats"] = ext_stats
                
                append_history({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "rule_name": r_name,
                    "status": "success",
                    "total_videos": len(videos),
                    "total_subs": len(subs),
                    "created": vc,
                    "skipped": vs,
                    "deleted": vd,
                    "elapsed_sec": elapsed,
                    "output_dir": out_dir,
                    "prefix": prefix,
                    "ext_stats": ext_stats
                })
            except Exception as e:
                r["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
                r["last_status"] = f"失败: {str(e)}"
                push_log(f"❌ 规则【{r_name}】执行失败: {str(e)}")
                append_history({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "rule_name": r_name,
                    "status": "error",
                    "message": str(e)
                })
            
            time.sleep(2)
                
        save_config(cfg)
        sync_progress["is_syncing"] = False
        sync_progress["stage"] = "就绪"
        push_log("======== 全部全量任务执行完毕 ========")

@app.get("/api/config")
def get_config():
    return load_config()

@app.post("/api/config")
def update_config(data: ConfigModel):
    cfg = load_config()
    cfg["cookie"] = data.cookie
    cfg["default_prefix"] = data.default_prefix
    cfg["default_output"] = data.default_output
    cfg["layer_limit"] = data.layer_limit
    cfg["sync_subtitles"] = data.sync_subtitles
    cfg["auto_cleanup_orphan"] = data.auto_cleanup_orphan
    cfg["cover_threads"] = data.cover_threads
    cfg["protected_categories"] = data.protected_categories
    cfg["ai_governance"] = data.ai_governance.dict()
    cfg["auto_sync_enabled"] = data.auto_sync_enabled
    cfg["auto_sync_interval_mins"] = data.auto_sync_interval_mins
    save_config(cfg)
    return {"status": "ok", "config": cfg}

# ----------------- 动态热调速 API -----------------

@app.get("/api/speed")
def get_speed():
    return {"delay": get_dynamic_delay()}

@app.post("/api/speed")
def set_speed(delay: float):
    safe_delay = max(MIN_SAFE_DELAY, float(delay))
    with open(SPEED_FILE, "w") as f:
        json.dump({"delay": safe_delay}, f)
    push_log(f"⚡ [流控调速生效] 抽帧延时已切换为: {safe_delay}s，后台流水线即时以该速度推进！")
    return {"status": "ok", "delay": safe_delay}

# ----------------- 二级专区 API -----------------


@app.get("/api/tasks")
def get_primary_tasks():
    cfg = load_config()
    rules = cfg.get("rules", [])
    res = []
    
    root_dir = os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人")
    total_videos = 0
    total_subs = 0
    total_covers = 0
    total_nfos = 0
    sub_categories_count = 0
    
    if os.path.exists(root_dir):
        subdirs = [d for d in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, d))]
        sub_categories_count = len(subdirs)
        for d in subdirs:
            p = os.path.join(root_dir, d)
            for root, _, files in os.walk(p):
                for f in files:
                    if f.endswith('.strm'):
                        m = re.search(r'\(([^)]+)\)\.strm$', f)
                        ext = ('.' + m.group(1).lower()) if m else ''
                        if ext in COMPREHENSIVE_SUBTITLE_EXTS:
                            total_subs += 1
                        else:
                            total_videos += 1
                    elif f.endswith('-poster.jpg') or f.endswith('-poster.png') or f.endswith('-poster.jpeg') or f.endswith('-poster.webp'):
                        total_covers += 1
                    elif f.endswith('.nfo'):
                        total_nfos += 1
                        
    cover_pct = round(total_covers / total_videos * 100) if total_videos > 0 else 100
    nfo_pct = round(total_nfos / total_videos * 100) if total_videos > 0 else 100
    
    res.append({
        "id": "chengren",
        "name": "成人",
        "description": f"{sub_categories_count} 个专属分类子专区 · 涵盖 3D动漫、FC2、欧美大厂、无码女优、女团与车模等",
        "badge": "核心媒体库",
        "path": "/Movies/TreeStrms/成人",
        "cid": "3291659674416491425",
        "sub_categories_count": sub_categories_count,
        "video_count": total_videos,
        "sub_count": total_subs,
        "cover_count": total_covers,
        "nfo_count": total_nfos,
        "cover_pct": cover_pct,
        "nfo_pct": nfo_pct,
        "last_sync": rules[0].get("last_sync", "实时") if rules else "实时",
        "status": "就绪",
        "status_color": "emerald"
    })
    
    res.append({
        "id": "movies_and_tv",
        "name": "影视剧集 & 动漫",
        "description": "电影、4K蓝光原盘、电视剧、短剧、国漫日漫等主流正剧库",
        "badge": "规划拓展中",
        "path": "/Movies/TreeStrms/影视",
        "cid": "2557717596187131421",
        "sub_categories_count": 0,
        "video_count": 0,
        "sub_count": 0,
        "cover_count": 0,
        "nfo_count": 0,
        "cover_pct": 0,
        "nfo_pct": 0,
        "last_sync": "待接入",
        "status": "模块待接入",
        "status_color": "slate"
    })
    
    return res

@app.post("/api/categories/scrape_official")
def trigger_official_scrape(category: str, max_items: int = 0):
    cfg = load_config()
    base_cat_dir = os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人", category)
    if not os.path.exists(base_cat_dir):
        return {"status": "error", "message": "专区路径不存在"}
        
    def _scrape_task():
        push_log(f"======== 🎬 启动专区【{category}】全源官方原画瀑布流刮削流水线 ========")
        cookie = cfg.get("cookie", "")
        session = create_115_session(cookie) if cookie else None
        ai_cfg = cfg.get("ai_governance", {})
        
        poster_count = 0
        scraped_count = 0
        failed_items = [] # 记录第一轮未命中的文件: (root, sf, base_fn, parent_dir)
        
        # ----------------- 【第 1 轮】：原生高置信度官方刮削 -----------------
        for root, dirs, files in os.walk(base_cat_dir):
            parent_dir = os.path.basename(root)
            strms = [f for f in files if f.endswith('.strm')]
            for sf in strms:
                base_fn = sf[:-5]
                poster_path = os.path.join(root, f"{base_fn}-poster.jpg")
                nfo_path = os.path.join(root, f"{base_fn}.nfo")
                
                need_poster = not os.path.exists(poster_path) and not os.path.exists(os.path.join(root, f"{base_fn}-poster.jpeg"))
                need_nfo = not os.path.exists(nfo_path)
                
                if need_poster or need_nfo:
                    scene = scraper_service.scrape_official_scene(base_fn, parent_dir)
                    if scene:
                        # 只要网络上确实抓到了真实官方元数据：
                        if need_poster:
                            p_url = scene.get('poster_url') or scene.get('poster') or (scene.get('background') or {}).get('full')
                            if p_url and scraper_service.download_and_save_poster(p_url, poster_path):
                                poster_count += 1
                                push_log(f"📸 [官方原画补齐] {base_fn} ➔ [{scene.get('title')}]")
                                
                        if need_nfo:
                            # 哪怕图片未取到，只要官方真实信息存在，坚决补齐真实正版 NFO！
                            nfo_content = scraper_service.generate_nfo_file_content(scene, base_fn)
                            with open(nfo_path, 'w', encoding='utf-8') as nfo_f:
                                nfo_f.write(nfo_content)
                            scraped_count += 1
                            push_log(f"📝 [正版NFO补齐] {base_fn} ➔ [{scene.get('title')}]")
                    else:
                        # 如果全网没有官方真实信息（例如番号写错或全网无记录），绝不强行生造假 NFO！
                        failed_items.append((root, sf, base_fn, parent_dir))
                    time.sleep(1.0)
                    if max_items > 0 and (poster_count + scraped_count) >= max_items:
                        break

        push_log(f"🎉 专区【{category}】官方瀑布流刮削完成！成功补齐海报: {poster_count} 张，补齐 NFO: {scraped_count} 个。共有 {len(failed_items)} 部未直接命中。")
        try:
            from services.emby_client import EmbyClient
            EmbyClient().trigger_category_refresh(category)
        except Exception:
            pass
        
    threading.Thread(target=_scrape_task, daemon=True).start()
    return {"status": "started", "message": f"已启动专区【{category}】官方瀑布流刮削流水线！"}



@app.get("/api/rules")
def get_rules_api():
    """
    通用对账任务管理列表：动态读取 config.json 中的所有 rules
    """
    cfg = load_config()
    rules = cfg.get("rules", [])
    output_base = cfg.get("default_output", "/Movies/TreeStrms")
    
    result = []
    for r in rules:
        r_name = r.get("name", "未命名任务")
        r_id = r.get("id", r_name)
        r_cid = r.get("cid", "")
        r_dir = os.path.join(output_base, r_name)
        
        # 统计该对账任务目录下的实际文件与子专区数
        v_count = 0
        c_count = 0
        n_count = 0
        sub_count = 0
        subdirs_list = []
        
        # 1. 优先读取秒级全量底账缓存（0.001秒极速返回，杜绝19万文件IO卡死）
        cache_file = os.path.join(DATA_DIR, "categories_cache.json")
        cached_map = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as cf:
                    cached_map = json.load(cf)
            except Exception:
                pass

        if cached_map:
            subdirs_list = sorted(list(cached_map.keys()))
            v_count = sum(x.get("video_count", 0) for x in cached_map.values())
            c_count = sum(x.get("cover_count", 0) for x in cached_map.values())
            n_count = sum(x.get("nfo_count", 0) for x in cached_map.values())
            sub_count = sum(x.get("sub_count", 0) for x in cached_map.values())
            if gov_progress.get("is_running"):
                live_nfos = gov_progress.get("nfos_generated", 0)
                live_covers = gov_progress.get("covers_extracted", 0)
                n_count += live_nfos
                c_count += live_covers
        elif os.path.exists(r_dir):
            subdirs = sorted([d for d in os.listdir(r_dir) if os.path.isdir(os.path.join(r_dir, d))])
            subdirs_list = subdirs
            v_set = set()
            c_set = set()
            n_set = set()
            for root, dirs, files in os.walk(r_dir):
                for f in files:
                    if f.endswith('.strm'):
                        m = re.search(r'\(([^)]+)\)\.strm$', f)
                        ext = ('.' + m.group(1).lower()) if m else ''
                        if ext in COMPREHENSIVE_SUBTITLE_EXTS:
                            sub_count += 1
                        else:
                            v_set.add(os.path.join(root, f[:-5]))
                    elif f.endswith('-poster.jpg') or f.endswith('-poster.png') or f.endswith('-poster.jpeg') or f.endswith('-poster.webp') or f.endswith('-poster.gif'):
                        m_base = re.sub(r'-poster\.(jpg|png|jpeg|webp|gif)$', '', f, flags=re.IGNORECASE)
                        c_set.add(os.path.join(root, m_base))
                    elif f.endswith('.nfo'):
                        n_set.add(os.path.join(root, f[:-4]))
            v_count = len(v_set)
            c_count = len(c_set.intersection(v_set))
            n_count = len(n_set.intersection(v_set))
                        
        c_pct = round(c_count / v_count * 100) if v_count > 0 else 100
        n_pct = round(n_count / v_count * 100) if v_count > 0 else 100
        
        result.append({
            "id": r_id,
            "name": r_name,
            "cid": r_cid,
            "prefix": r.get("prefix", "/movies/CloudDrive/115"),
            "output_dir": r.get("output_dir", output_base),
            "enabled": r.get("enabled", True),
            "last_sync": r.get("last_sync", "未同步"),
            "last_status": r.get("last_status", "就绪"),
            "video_count": v_count if v_count > 0 else r.get("video_count", 0),
            "sub_count": sub_count if sub_count > 0 else r.get("sub_count", 0),
            "cover_count": c_count,
            "nfo_count": n_count,
            "cover_pct": c_pct,
            "nfo_pct": n_pct,
            "sub_categories_count": len(subdirs_list)
        })
    return result

@app.post("/api/rules/add")
def add_rule_api(payload: dict):
    cfg = load_config()
    rules = cfg.get("rules", [])
    name = payload.get("name", "").strip()
    cid = payload.get("cid", "").strip()
    if not name or not cid:
        raise HTTPException(status_code=400, detail="任务名称与 115 目录 CID 不能为空！")
    
    # 检查重名
    for r in rules:
        if r.get("name") == name or r.get("cid") == cid:
            raise HTTPException(status_code=400, detail=f"已存在同名或同 CID 任务：{name}")
            
    new_rule = {
        "id": f"rule_{int(time.time())}",
        "name": name,
        "cid": cid,
        "output_dir": payload.get("output_dir", cfg.get("default_output", "/Movies/TreeStrms")),
        "prefix": payload.get("prefix", cfg.get("default_prefix", "/movies/CloudDrive/115")),
        "sync_subtitles": True,
        "enabled": True,
        "last_sync": "刚刚创建",
        "last_status": "就绪",
        "video_count": 0,
        "sub_count": 0
    }
    rules.append(new_rule)
    cfg["rules"] = rules
    save_config(cfg)
    push_log(f"➕ 新增通用对账任务【{name}】(CID: {cid}) 成功！")
    return {"status": "ok", "rule": new_rule}

@app.delete("/api/rules/{rule_id}")
def delete_rule_api(rule_id: str):
    cfg = load_config()
    rules = cfg.get("rules", [])
    rules = [r for r in rules if r.get("id") != rule_id and r.get("name") != rule_id]
    cfg["rules"] = rules
    save_config(cfg)
    push_log(f"🗑️ 已删除对账任务: {rule_id}")
    return {"status": "ok"}

@app.get("/api/categories")
def get_categories():
    cfg = load_config()
    root_dir = os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人")
    protected_set = set(cfg.get("protected_categories", DEFAULT_PROTECTED_CATEGORIES))
    
    if not os.path.exists(root_dir):
        return []
        
    res = []
    subdirs = sorted([d for d in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, d))])
    
    running_cat = current_running_job.get("category") if current_running_job else None
    queued_cats = [q.get("category") for q in governance_queue]
    
    # 优先使用秒级全量底账缓存
    cache_file = os.path.join(DATA_DIR, "categories_cache.json")
    cached_map = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as cf:
                cached_map = json.load(cf)
        except Exception:
            pass

    for d in subdirs:
        p = os.path.join(root_dir, d)
        if d in cached_map:
            item = cached_map[d].copy()
            v_count = item.get("video_count", 0)
            sub_count = item.get("sub_count", 0)
            c_count = item.get("cover_count", 0)
            n_count = item.get("nfo_count", 0)
        else:
            sub_count = 0
            v_set = set()
            c_set = set()
            n_set = set()
            for root, _, files in os.walk(p):
                for f in files:
                    if f.endswith('.strm'):
                        v_set.add(os.path.join(root, f[:-5]))
                    elif re.search(r'-(poster|thumb)\.(jpg|png|jpeg|webp|gif)$', f, re.IGNORECASE):
                        m_base = re.sub(r'-(poster|thumb)\.(jpg|png|jpeg|webp|gif)$', '', f, flags=re.IGNORECASE)
                        c_set.add(os.path.join(root, m_base))
                    elif f.endswith('.nfo'):
                        n_set.add(os.path.join(root, f[:-4]))
                    elif any(f.endswith(ext) for ext in COMPREHENSIVE_SUBTITLE_EXTS):
                        sub_count += 1
            v_count = len(v_set)
            c_count = len(c_set.intersection(v_set))
            n_count = len(n_set.intersection(v_set))
                
        is_scraped = d in protected_set
        status_state = "idle"
        status_text = "商业已刮削 (锁定保护)" if is_scraped else "原生待治理 (已解锁)"
        
        # 动态融合当前治理任务实时计数
        if d == running_cat:
            status_state = "running"
            status_text = "正在查漏补缺中..."
            if gov_progress.get("is_running"):
                live_nfos = gov_progress.get("nfos_generated", 0)
                live_covers = gov_progress.get("covers_extracted", 0)
                n_count = max(n_count, live_nfos)
                c_count = min(v_count, c_count + live_covers)
        elif d in queued_cats:
            pos = queued_cats.index(d) + 1
            status_state = "queued"
            status_text = f"已加入排队 (第 {pos} 位)"
            
        res.append({
            "name": d,
            "path": p,
            "video_count": v_count,
            "sub_count": sub_count,
            "cover_count": c_count,
            "nfo_count": n_count,
            "is_scraped": is_scraped,
            "type_desc": status_text,
            "status_state": status_state
        })
    return res

@app.post("/api/categories/toggle_lock")
def toggle_category_lock(category: str):
    cfg = load_config()
    protected_list = cfg.get("protected_categories", DEFAULT_PROTECTED_CATEGORIES)
    if category in protected_list:
        protected_list.remove(category)
        push_log(f"🔓 已手动【解锁】专区【{category}】，现已开放 AI 治理与封面抽帧！")
        status = "unlocked"
    else:
        protected_list.append(category)
        push_log(f"🔒 已手动【加锁】保护专区【{category}】，原版封面受到保护！")
        status = "locked"
    cfg["protected_categories"] = protected_list
    save_config(cfg)
    return {"status": "ok", "category": category, "is_scraped": (status == "locked")}

@app.post("/api/categories/clean_orphan")
def trigger_clean_orphan(category: str):
    res = clean_orphan_metadata_for_category(category)
    return {"status": "ok", "result": res}

@app.post("/api/categories/governance")
def trigger_category_governance(category: str, extract_covers: bool = True, probe_streams: bool = False, max_items: int = 0):
    if current_running_job and current_running_job.get("category") == category:
        return {"status": "running", "message": f"专区【{category}】当前正在查漏补缺推进中！"}
        
    for q in governance_queue:
        if q.get("category") == category:
            return {"status": "already_queued", "message": f"专区【{category}】已在排队队列中，请耐心等待自动执行！"}
            
    with gov_lock:
        governance_queue.append({
            "category": category,
            "extract_covers": extract_covers,
            "probe_streams": probe_streams,
            "max_items": max_items
        })
        gov_progress["queue_length"] = len(governance_queue)
        
    if not current_running_job:
        push_log(f"📥 专区【{category}】已提交，立即启动 CD2 治理执行！")
        return {"status": "started", "message": f"专区【{category}】立即启动 CD2 治理执行！"}
    else:
        pos = len(governance_queue)
        push_log(f"📥 专区【{category}】已加入自动排队序列 (排在第 {pos} 位)！")
        return {"status": "queued", "message": f"已将【{category}】加入排队序列 (第 {pos} 位)！当前专区完成后将自动无缝接力推进！"}

@app.delete("/api/categories/governance/queue/{category}")
def remove_category_from_queue(category: str):
    global governance_queue
    with gov_lock:
        original_len = len(governance_queue)
        governance_queue = [q for q in governance_queue if q.get("category") != category]
        gov_progress["queue_length"] = len(governance_queue)
        if len(governance_queue) < original_len:
            push_log(f"🗑️ 已成功从排队序列中移除专区【{category}】")
            return {"status": "success", "message": f"已成功从排队序列中移除【{category}】！"}
        return {"status": "not_found", "message": f"专区【{category}】不在排队序列中"}

@app.post("/api/sync")
def trigger_sync(background_tasks: BackgroundTasks, rule_id: Optional[str] = None):
    if sync_progress["is_syncing"]:
        return {"status": "busy", "message": "已有全量同步正在进行中..."}
    ids = [rule_id] if rule_id else None
    background_tasks.add_task(do_sync_task, ids)
    return {"status": "started", "message": "全量对账任务已在后台启动！"}

@app.get("/api/status")
def get_status():
    return {
        "sync_progress": sync_progress,
        "gov_progress": gov_progress,
        "current_running_job": current_running_job,
        "governance_queue": governance_queue,
        "live_logs": live_log_messages[-80:],
        "history": load_history()[:15],
        "current_delay": get_dynamic_delay()
    }

@app.get("/", response_class=HTMLResponse)
def index_page():
    html_path = os.path.join(BASE_DIR, "static", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>TreeSTRM 控制台初始化中...</h1>"

def background_scheduler():
    while True:
        try:
            cfg = load_config()
            if cfg.get("auto_sync_enabled"):
                interval_mins = max(5, int(cfg.get("auto_sync_interval_mins") or 30))
                do_sync_task()
                time.sleep(interval_mins * 60)
            else:
                time.sleep(30)
        except Exception:
            time.sleep(30)

threading.Thread(target=background_scheduler, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8028)
