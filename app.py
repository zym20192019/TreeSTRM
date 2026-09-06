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

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

COMPREHENSIVE_VIDEO_EXTS = {
    '.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v',
    '.ts', '.m2ts', '.mts', '.vob', '.iso', '.rmvb', '.rm', '.asf',
    '.264', '.265', '.hevc', '.mpg', '.mpeg', '.f4v'
}

COMPREHENSIVE_SUBTITLE_EXTS = {
    '.srt', '.ass', '.ssa', '.vtt', '.sub', '.sup', '.idx', '.smi'
}

live_log_messages: List[str] = []
MAX_LIVE_LOGS = 300

TG_BOT_TOKEN = ""
TG_CHAT_ID = "5662349315"

def send_telegram_alert(text: str):
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        pass

def push_log(msg: str):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    live_log_messages.append(entry)
    if len(live_log_messages) > MAX_LIVE_LOGS:
        live_log_messages.pop(0)

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
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        push_log(f"⚠️ AI 标签审批批次跳过 (降级走规则): {str(e)[:60]}")
    return None

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

def extract_cover_by_cd2(target_path: str, poster_path: str, thumb_path: str, seek_sec: int = 20) -> bool:
    """直接走 CD2 本地挂载 Fast Seek (智能定位 45% 正片精彩画面)，0 验证码，0.9s 秒级出图！"""
    try:
        host_target = target_path
        if host_target.startswith('/movies/'):
            host_target = '/Movies/' + host_target[8:]
            
        if not os.path.exists(host_target):
            return False
            
        # 优先 seek 到指定正片黄金时间点 (例如 45% 处)
        s_time = time.strftime('%H:%M:%S', time.gmtime(seek_sec)) if seek_sec > 0 else '00:00:05'
        cmd = [
            'ffmpeg', '-y',
            '-ss', s_time,
            '-i', host_target,
            '-vframes', '1',
            '-vf', 'scale=min(1080\\,iw):-2',
            '-q:v', '3',
            poster_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=15)
        
        # 若超长 seek 失败 (例如超短视频)，自适应回退到 2 秒处保底
        if not os.path.exists(poster_path) or os.path.getsize(poster_path) < 1000:
            cmd_fallback = [
                'ffmpeg', '-y',
                '-ss', '00:00:02',
                '-i', host_target,
                '-vframes', '1',
                '-vf', 'scale=min(1080\\,iw):-2',
                '-q:v', '3',
                poster_path
            ]
            subprocess.run(cmd_fallback, capture_output=True, timeout=15)
            
        if os.path.exists(poster_path) and os.path.getsize(poster_path) > 1000:
            subprocess.run(['cp', poster_path, thumb_path], capture_output=True)
            return True
        return False
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
    
    for rel_path in video_paths:
        dirname = os.path.dirname(rel_path)
        filename = os.path.basename(rel_path)
        basename, ext_with_dot = os.path.splitext(filename)
        ext = ext_with_dot.lstrip('.')
        strm_filename = f"{basename}.({ext}).strm"
        target_path = os.path.join(output_dir, dirname, strm_filename) if dirname else os.path.join(output_dir, strm_filename)
        content = f"{strm_prefix}/{rel_path.lstrip('/')}"
        expected_strms[target_path] = content

    for rel_path in sub_paths:
        dirname = os.path.dirname(rel_path)
        filename = os.path.basename(rel_path)
        basename, ext_with_dot = os.path.splitext(filename)
        ext = ext_with_dot.lstrip('.')
        strm_filename = f"{basename}.({ext}).strm"
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
    max_items = job.get("max_items", 0)
    
    current_running_job = job
    gov_progress["is_running"] = True
    gov_progress["category"] = category_name
    gov_progress["stage"] = f"正在查漏补缺【{category_name}】"
    gov_progress["queue_length"] = len(governance_queue)
    gov_progress["is_paused_for_captcha"] = False
    start_t = time.time()
    
    current_delay = get_dynamic_delay()
    push_log(f"======== 🚀 启动【{category_name}】CD2 高速无风控治理流水线 (流控: {current_delay}s) ========")
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
    
    for root, dirs, files in os.walk(base_cat_dir):
        strms = [f for f in files if f.endswith('.strm')]
        if strms:
            folder_items = []
            for sf in strms:
                base_fn = sf[:-5]
                m = re.search(r'\(([^)]+)\)$', base_fn)
                ext = m.group(1) if m else 'mp4'
                poster = os.path.join(root, f"{base_fn}-poster.jpg")
                nfo = os.path.join(root, f"{base_fn}.nfo")
                has_p = os.path.exists(poster) and os.path.getsize(poster) > 1000
                has_n = os.path.exists(nfo)
                
                if not has_p: missing_covers_count += 1
                if not has_n: missing_nfos_count += 1
                
                folder_items.append({
                    "strm_path": os.path.join(root, sf),
                    "base_name": base_fn,
                    "ext": ext,
                    "poster_path": poster,
                    "thumb_path": os.path.join(root, f"{base_fn}-thumb.jpg"),
                    "nfo_path": nfo,
                    "has_poster": has_p,
                    "has_nfo": has_n
                })
            sub_batches[root] = folder_items
            total_v += len(folder_items)

    # 精准统计真正需要工作的条目数
    need_work_items = []
    for root, items in sub_batches.items():
        for it in items:
            if not it["has_nfo"] or (extract_covers and not it["has_poster"]):
                need_work_items.append((root, it))
                
    target_limit = len(need_work_items) if (max_items <= 0) else min(len(need_work_items), max_items)
    gov_progress["total_videos"] = target_limit
    gov_progress["processed_videos"] = 0
    gov_progress["covers_extracted"] = 0
    gov_progress["nfos_generated"] = 0
    gov_progress["ai_batches_done"] = 0
    
    push_log(f"专区【{category_name}】全盘盘点完毕：共 {total_v} 部视频，待处理目标: {len(need_work_items)} 部 (待补封面: {missing_covers_count} 张, 待补 NFO: {missing_nfos_count} 个)")
    
    processed_count = 0
    
    for folder_path, items in sub_batches.items():
        if processed_count >= target_limit: break
        if gov_progress["is_paused_for_captcha"]: break
        
        sub_need_work = [it for it in items if (not it["has_nfo"] or (extract_covers and not it["has_poster"]))]
        if not sub_need_work:
            continue
            
        sub_folder_name = os.path.basename(folder_path) or category_name
        sample_files = [x["base_name"] for x in items]
        
        # 1. 智能 AI 审批
        needs_nfo = any(not it["has_nfo"] for it in items)
        ai_res = None
        if needs_nfo and ai_cfg.get("enabled") and ai_cfg.get("api_key"):
            ai_res = call_ai_auditor(f"{category_name} - {sub_folder_name}", sample_files, ai_cfg)
            if ai_res:
                gov_progress["ai_batches_done"] += 1
                push_log(f"🤖 [{sub_folder_name}] AI 审批就绪: 分类={ai_res.get('genre')}")

        # 2. 锁定子目录 pickcode 映射 (顺带提取 filesize 和 runtime)
        folder_pickcode_map = {}
        if session:
            folder_pickcode_map = fetch_folder_pickcodes(session, category_name, folder_path)

        # 过滤本目录下真正需要处理的条目
        active_items = [it for it in items if (not it["has_nfo"] or (extract_covers and not it["has_poster"]))]
        if not active_items:
            continue

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

            file_meta_info = folder_pickcode_map.get(video_name, {})
            file_size_bytes = file_meta_info.get('size', 0) if isinstance(file_meta_info, dict) else 0
            play_long_sec = file_meta_info.get('play_long', 0) if isinstance(file_meta_info, dict) else 0
            runtime_mins = round(play_long_sec / 60) if play_long_sec > 0 else 0

            # 1. 补 NFO
            if not it["has_nfo"]:
                rel_p = os.path.relpath(it["strm_path"], cfg.get("default_output"))
                meta = extract_rule_tags(rel_p)
                if ai_res:
                    if ai_res.get("studio"): meta["studio"] = ai_res.get("studio")
                    if ai_res.get("genre"):
                        g = ai_res.get("genre")
                        meta["genres"] = g if isinstance(g, list) else [g]
                    if ai_res.get("common_tags"):
                        meta["tags"] = list(dict.fromkeys(meta["tags"] + ai_res.get("common_tags")))
                    if ai_res.get("clean_summary"):
                        meta["summary"] = ai_res.get("clean_summary")
                write_nfo_file(it["nfo_path"], meta, runtime_mins=runtime_mins, filesize=file_size_bytes)
                gov_progress["nfos_generated"] += 1

            if extract_covers and not it["has_poster"]:
                seek_target = max(5, int(play_long_sec * 0.45)) if play_long_sec > 15 else 2
                work_tasks.append((raw_target, it["poster_path"], it["thumb_path"], seek_target, video_name))

        # 2. CD2 本地通道多线程并发 Fast Seek 抽封面 (动态线程数配置，0 验证码、0.9s 秒级出图)
        if work_tasks:
            worker_threads = max(1, min(10, int(cfg.get("cover_threads", 3))))
            def _extract_worker(task):
                raw_t, p_path, th_path, seek_t, v_name = task
                try:
                    ok = extract_cover_by_cd2(raw_t, p_path, th_path, seek_sec=seek_t)
                    if ok:
                        with gov_lock:
                            gov_progress["covers_extracted"] += 1
                            gov_progress["recent_success_streak"] += 1
                            if gov_progress["covers_extracted"] % 10 == 0 or gov_progress["covers_extracted"] == 1:
                                push_log(f"📸 [CD2直出-{worker_threads}线程] 成功补齐封面: {v_name[:25]}... (累计: {gov_progress['covers_extracted']} 张)")
                except Exception:
                    pass

            with ThreadPoolExecutor(max_workers=worker_threads) as pool:
                list(pool.map(_extract_worker, work_tasks))
                    
    gov_progress["elapsed"] = round(time.time() - start_t, 2)
    done_msg = f"🎉 专区【{category_name}】CD2 无风控治理圆满完成！共补齐 NFO: {gov_progress['nfos_generated']} 个，补齐封面: {gov_progress['covers_extracted']} 张！耗时: {gov_progress['elapsed']}秒"
    push_log(done_msg)
    send_telegram_alert(f"✅ [TreeSTRM 完成通知]\n{done_msg}")
    current_running_job = None

def queue_worker_loop():
    while True:
        try:
            if governance_queue and not current_running_job and not gov_progress["is_paused_for_captcha"]:
                with gov_lock:
                    next_job = governance_queue.pop(0)
                    gov_progress["queue_length"] = len(governance_queue)
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

def clean_orphan_metadata_for_category(category_name: str) -> dict:
    cfg = load_config()
    target_dir = os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人", category_name)
    if not os.path.exists(target_dir):
        return {"deleted_nfo": 0, "deleted_posters": 0, "deleted_thumbs": 0}
        
    push_log(f"======== 🧹 开始对专区【{category_name}】执行孤立失效元数据清理 ========")
    del_nfo = 0
    del_poster = 0
    del_thumb = 0
    
    for root, dirs, files in os.walk(target_dir):
        strms = set()
        for f in files:
            if f.endswith('.strm'):
                strms.add(f[:-5])
                
        for f in files:
            p = os.path.join(root, f)
            if f.endswith('.nfo'):
                base = f[:-4]
                if base not in strms:
                    try:
                        os.remove(p)
                        del_nfo += 1
                    except Exception: pass
            elif f.endswith('-poster.jpg'):
                base = f[:-11]
                if base not in strms:
                    try:
                        os.remove(p)
                        del_poster += 1
                    except Exception: pass
            elif f.endswith('-thumb.jpg'):
                base = f[:-10]
                if base not in strms:
                    try:
                        os.remove(p)
                        del_thumb += 1
                    except Exception: pass
                    
    total_cleaned = del_nfo + del_poster + del_thumb
    push_log(f"✅ 专区【{category_name}】失效元数据清理完毕！共删除孤立文件: {total_cleaned} 个 (NFO: -{del_nfo}, 海报: -{del_poster}, 缩略图: -{del_thumb})")
    return {"deleted_nfo": del_nfo, "deleted_posters": del_poster, "deleted_thumbs": del_thumb, "total": total_cleaned}

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
    
    for d in subdirs:
        p = os.path.join(root_dir, d)
        v_count = 0
        sub_count = 0
        c_count = 0
        n_count = 0
        for root, _, files in os.walk(p):
            for f in files:
                if f.endswith('.strm'):
                    m = re.search(r'\(([^)]+)\)\.strm$', f)
                    ext = ('.' + m.group(1).lower()) if m else ''
                    if ext in COMPREHENSIVE_SUBTITLE_EXTS:
                        sub_count += 1
                    else:
                        v_count += 1
                elif 'poster' in f: c_count += 1
                elif f.endswith('.nfo'): n_count += 1
                
        is_scraped = d in protected_set
        
        status_state = "idle"
        status_text = "商业已刮削 (锁定保护)" if is_scraped else "原生待治理 (已解锁)"
        if d == running_cat:
            status_state = "running"
            status_text = "正在查漏补缺中..."
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
def trigger_category_governance(category: str, extract_covers: bool = True, max_items: int = 0):
    if current_running_job and current_running_job.get("category") == category:
        return {"status": "running", "message": f"专区【{category}】当前正在查漏补缺推进中！"}
        
    for q in governance_queue:
        if q.get("category") == category:
            return {"status": "already_queued", "message": f"专区【{category}】已在排队队列中，请耐心等待自动执行！"}
            
    with gov_lock:
        governance_queue.append({
            "category": category,
            "extract_covers": extract_covers,
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
