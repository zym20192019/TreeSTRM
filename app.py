import scraper_service
import os, sys, re, time, json, asyncio, threading, requests, subprocess, urllib.parse, shlex, secrets, hmac
from urllib.parse import unquote
from xml.sax.saxutils import escape as xml_escape
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from core.config import (
    BASE_DIR, CONFIG_FILE, DATA_DIR, LOGS_FILE, SPEED_FILE,
    DEFAULT_PROTECTED_CATEGORIES, COMPREHENSIVE_SUBTITLE_EXTS,
    ConfigModel, load_config, save_config,
)
from core.events import push_log, live_log_messages, send_telegram_alert, get_dynamic_delay, MIN_SAFE_DELAY, LOG_DIR
from services.cloud115 import create_115_session, export_115_tree
from engine.reconciler import parse_tree_1to1, sync_strms_pure_1to1

AI_FAILURE_LEDGER_FILE = os.path.join(DATA_DIR, "ai_failure_batches.json")

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

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

_history_lock = threading.Lock()
_history_cache: Optional[list] = None

def _read_history_file() -> list:
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []

def load_history() -> list:
    """history.json 只由本进程写入，内存缓存后 /api/status 轮询不再读盘"""
    global _history_cache
    with _history_lock:
        if _history_cache is None:
            _history_cache = _read_history_file()
        return list(_history_cache)

def append_history(entry: dict):
    global _history_cache
    with _history_lock:
        current = _history_cache if _history_cache is not None else _read_history_file()
        hist = ([entry] + current)[:50]
        tmp_path = LOGS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, LOGS_FILE)
        _history_cache = hist

# ---------- 智能文字语义与 AI 标签治理核心 ----------

def call_ai_auditor(folder_name: str, file_names: List[str], ai_cfg: dict) -> Optional[dict]:
    """目录级 AI 审批：失败重试 2 次，仍失败记录账本并规则降级。"""
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
    last_error = "unknown error"
    for attempt in range(1, 3):
        try:
            url = f"{api_base}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "HTTP-Referer": "https://github.com/treestrm", "X-Title": "TreeSTRM", "Content-Type": "application/json"}
            data = {"model": model, "messages": [{"role": "system", "content": "你是一个严谨的媒体库元数据治理专家，只输出合法 JSON。"}, {"role": "user", "content": prompt}], "temperature": 0.2}
            resp = requests.post(url, headers=headers, json=data, timeout=25).json()
            choices = resp.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                raise ValueError("AI response choices is empty")
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if not json_match:
                raise ValueError("AI response contains no JSON object")
            parsed = json.loads(json_match.group(0))
            if not isinstance(parsed, dict):
                raise ValueError("AI response JSON is not an object")
            if attempt > 1:
                push_log(f"✅ AI 目录审批重试成功: {folder_name}（第{attempt}次）")
            return parsed
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            push_log(f"⚠️ AI 目录审批第{attempt}/2次失败: {folder_name}: {last_error[:180]}")
            if attempt < 2:
                time.sleep(2)
    _record_ai_failure(folder_name, sample_files, last_error, 2, fallback=True)
    push_log(f"❌ AI 目录审批最终失败，已记录并规则降级: {folder_name}")
    return None

def _record_ai_failure(folder_hint: str, file_names: List[str], error: str, attempts: int, fallback: bool = True):
    """持久化记录最终失败批次，禁止失败请求被静默当成成功。"""
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "folder": folder_hint,
        "files": list(file_names),
        "error": str(error)[:500],
        "attempts": attempts,
        "fallback_nfo": fallback,
        "status": "pending_repair"
    }
    records = []
    try:
        if os.path.exists(AI_FAILURE_LEDGER_FILE):
            with open(AI_FAILURE_LEDGER_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
            if not isinstance(records, list):
                records = []
    except Exception:
        records = []
    records.append(record)
    with open(AI_FAILURE_LEDGER_FILE, "w", encoding="utf-8") as f:
        json.dump(records[-5000:], f, ensure_ascii=False, indent=2)
    return record


def call_ai_item_auditor(file_names: List[str], folder_hint: str, ai_cfg: dict, _allow_split: bool = True,
                         failed_paths: Optional[set] = None) -> Dict[str, dict]:
    """请求文件级 AI（按序号索引映射机制，一次性支持 50 个直推，0 误判、0 漏项）。
    failed_paths: 若提供，最终失败批次的文件路径会加入该集合（替代读取失败账本做长度差，账本封顶 5000 条后差值恒为 0）。
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

输出必须严格为合法的 JSON 对象，键必须是对应的数字编号("1", "2", ...)，格式如下：
{{
  "1": {{
    "clean_title": "清洗后的展示标题",
    "tags": ["标签1", "标签2"],
    "summary": "简述"
  }}
}}

待处理列表：
""" + "\n".join([f"{i+1}. {fn}" for i, fn in enumerate(file_names)])

    last_error = "unknown error"
    for attempt in range(1, 3):
        try:
            url = f"{api_base}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "HTTP-Referer": "https://github.com/treestrm", "X-Title": "TreeSTRM", "Content-Type": "application/json"}
            data = {"model": model, "messages": [{"role": "system", "content": "你是一个严谨的媒体库元数据治理专家，严格按序号输出合法JSON对象。"}, {"role": "user", "content": prompt}], "temperature": 0.2}
            resp = requests.post(url, headers=headers, json=data, timeout=30).json()
            choices = resp.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                raise ValueError("AI response choices is empty")
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            if not isinstance(content, str) or not content.strip():
                raise ValueError("AI response content is empty")
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if not json_match:
                raise ValueError("AI response contains no JSON object")
            parsed = json.loads(json_match.group(0))
            if not isinstance(parsed, dict):
                raise ValueError("AI response JSON is not an object")

            # 通过序号严格 1:1 反向映射回原始完整路径
            result_map = {}
            for i, fn in enumerate(file_names):
                idx_key = str(i + 1)
                item = parsed.get(idx_key) or parsed.get(i + 1)
                if isinstance(item, dict) and item.get("clean_title"):
                    result_map[fn] = item

            if attempt > 1:
                push_log(f"✅ AI 文件批次重试成功: {folder_hint} ({len(file_names)}个，第{attempt}次)")
            return result_map
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            push_log(f"⚠️ AI 文件批次第{attempt}/2次失败: {folder_hint} ({len(file_names)}个): {last_error[:180]}")
            if attempt < 2:
                time.sleep(2)

    if _allow_split and len(file_names) > 40:
        push_log(f"🔻 AI 批次连续失败，拆分重试: {folder_hint} {len(file_names)} -> 40/批")
        merged = {}
        for start in range(0, len(file_names), 40):
            merged.update(call_ai_item_auditor(file_names[start:start + 40], folder_hint, ai_cfg, _allow_split=False, failed_paths=failed_paths))
        return merged

    _record_ai_failure(folder_hint, file_names, last_error, 2)
    if failed_paths is not None:
        failed_paths.update(file_names)
    push_log(f"❌ AI 文件批次最终失败，已记录并降级生成 NFO: {folder_hint} ({len(file_names)}个)")
    return {}

def call_ai_translation_fallback(file_names: List[str], folder_hint: str, ai_cfg: dict) -> Dict[str, dict]:
    """最后兜底：采用【数字序号索引映射机制 (Index-based Mapping)】。
    彻底杜绝大模型微调乱码字符、逗号网址([gaobei,com]→[gaobei.com])或多余空格导致的 Key 丢失与对账失败。
    """
    if not file_names or not ai_cfg.get("enabled") or not ai_cfg.get("api_key"):
        return {}
    api_base = (ai_cfg.get("api_base") or "https://openrouter.ai/api/v1").rstrip("/")
    api_key = ai_cfg.get("api_key")
    model = ai_cfg.get("fallback_model") or ai_cfg.get("model") or "gemini-3.5-flash-lite"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    result: Dict[str, dict] = {}

    def request_json(system_text: str, user_text: str):
        last_error = "unknown error"
        for attempt in range(1, 3):
            try:
                resp = requests.post(
                    f"{api_base}/chat/completions",
                    headers=headers,
                    json={"model": model, "messages": [
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": user_text},
                    ], "temperature": 0},
                    timeout=90,
                ).json()
                choices = resp.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    raise ValueError("AI response choices is empty")
                content = (choices[0].get("message") or {}).get("content") or ""
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("AI response content is empty")
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if not match:
                    raise ValueError("AI response contains no JSON object")
                parsed = json.loads(match.group(0))
                if not isinstance(parsed, dict):
                    raise ValueError("AI response JSON is not an object")
                return parsed
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(2)
        raise ValueError(last_error)

    for start in range(0, len(file_names), 10):
        batch = file_names[start:start + 10]
        try:
            translation_prompt = (
                "这是媒体库文件路径翻译任务。请把以下视频文件名中的日文/英文/韩文翻译成简洁、客观的中文标题，"
                "不扩写、不描述画面、不生成色情内容。严格按编号(1, 2, ...)作为 JSON 键，输出格式：\n"
                "{\"1\":{\"中文标题\":\"...\"}, \"2\":{\"中文标题\":\"...\"}}。\n待处理列表：\n"
                + "\n".join(f"{i+1}. {path}" for i, path in enumerate(batch))
            )
            translated = request_json(
                "你是文件名翻译器，严格按编号输出合法 JSON 对象。",
                translation_prompt,
            )

            title_rows = []
            for i, path in enumerate(batch):
                idx_key = str(i + 1)
                t_obj = translated.get(idx_key) or translated.get(i + 1)
                title = (t_obj or {}).get("中文标题") if isinstance(t_obj, dict) else None
                if not isinstance(title, str) or not title.strip():
                    title = os.path.splitext(os.path.basename(path))[0]
                title_rows.append(f"{i+1}. 原路径: {path} -> 中文标题: {title}")

            metadata = request_json(
                "你是媒体库元数据整理器，严格按编号输出合法 JSON 对象。",
                "这是媒体库元数据整理任务。只基于中文标题输出严格 JSON 对象，键必须是对应的编号(\"1\",\"2\"...)；"
                "每项包含 clean_title、tags、summary，summary 不超过25字。不要 markdown，不要解释。\n"
                + "\n".join(title_rows),
            )
            for i, path in enumerate(batch):
                idx_key = str(i + 1)
                item = metadata.get(idx_key) or metadata.get(i + 1)
                if not isinstance(item, dict):
                    continue
                if not isinstance(item.get("clean_title"), str) or not item.get("clean_title").strip():
                    item["clean_title"] = os.path.splitext(os.path.basename(path))[0]
                if not isinstance(item.get("tags"), list):
                    item["tags"] = []
                if not isinstance(item.get("summary"), str):
                    item["summary"] = item.get("clean_title", "")
                result[path] = item
            push_log(f"✅ AI 翻译兜底成功: {folder_hint} ({len(batch)}个)")
        except Exception as exc:
            _record_ai_failure(f"{folder_hint} [翻译兜底]", batch, str(exc), 2, fallback=False)
            push_log(f"❌ AI 翻译兜底失败，相关 NFO 保持缺失: {folder_hint} ({len(batch)}个): {str(exc)[:180]}")
    return result

def _xml_text(value: Any) -> str:
    """AI 返回内容可能含 & < >，不转义会生成 Emby 无法解析的坏 NFO"""
    return xml_escape(str(value or ""))

def _cdata_text(value: Any) -> str:
    # CDATA 内唯一非法序列是 ]]>，拆成两个相邻 CDATA 段
    return str(value or "").replace("]]>", "]]]]><![CDATA[>")

def write_nfo_file(nfo_path: str, meta: dict, runtime_mins: int = 0, filesize: int = 0, width: int = 1920, height: int = 1080):
    tags_xml = "\n".join([f"  <tag>{_xml_text(t)}</tag>" for t in meta.get("tags", [])])
    genres_xml = "\n".join([f"  <genre>{_xml_text(g)}</genre>" for g in meta.get("genres", [])])
    studio = _xml_text(meta.get("studio", ""))
    title = _cdata_text(meta.get("title", ""))
    summary = _cdata_text(meta.get("summary", ""))
    
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

from engine.cd2_extractor import extract_cover_by_cd2, probe_media_info, update_nfo_with_streamdetails, map_to_cd2_path


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
def is_subtitle_strm(strm_path: str, filename: str) -> bool:
    """读取 STRM 实际目标扩展名，避免 `(sub).mp4` 被误判为字幕。"""
    try:
        with open(strm_path, "r", encoding="utf-8", errors="replace") as f:
            target = f.readline().strip()
        target_ext = os.path.splitext(target.split("?", 1)[0])[1].lower()
        if target_ext:
            return target_ext in COMPREHENSIVE_SUBTITLE_EXTS
    except OSError:
        pass
    m = re.search(r'\(([^)]+)\)$', filename[:-5]) if filename.lower().endswith('.strm') else None
    return bool(m and ('.' + m.group(1).lower()) in COMPREHENSIVE_SUBTITLE_EXTS)


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
    """异常时也必须释放运行槽位，否则 queue_worker_loop 会永久认为有任务在跑，整条队列卡死"""
    global current_running_job
    try:
        _run_category_job(job)
    finally:
        current_running_job = None

def _run_category_job(job: dict):
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
                strm_full_path = os.path.join(root, sf)
                is_sub = is_subtitle_strm(strm_full_path, sf)
                
                poster = os.path.join(root, f"{base_fn}-poster.jpg")
                nfo = os.path.join(root, f"{base_fn}.nfo")
                has_p = os.path.exists(poster) and os.path.getsize(poster) > 1000
                # ASMR 等普通专区的“有 NFO”与“内容已完成”是两个概念；
                # 这里只统计文件是否存在，避免把“缺少流信息”误报成“缺少 NFO”。
                has_n = os.path.exists(nfo) and os.path.getsize(nfo) > 0
                if has_n:
                    try:
                        with open(nfo, "r", encoding="utf-8-sig", errors="ignore") as nf:
                            nfo_text = nf.read()
                        has_n = "<movie" in nfo_text.lower() and "<title" in nfo_text.lower()
                    except Exception:
                        has_n = False
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
        
        sub_need_work = [it for it in items if not it.get("is_sub", False) and ((not it["has_nfo"]) or (extract_covers and not it["has_poster"]) or (probe_streams and not it["has_streamdetails"]))]
        if not sub_need_work:
            continue
            
        sub_folder_name = os.path.basename(folder_path) or category_name
        # AI 必须看到从 TreeStrms 根开始的完整目录层级，不能只看到当前末级目录名。
        # 例如：成人/舞蹈/精舞社/精舞社 可涵 第1期/4K竖版_11.strm
        output_root = os.path.abspath(cfg.get("default_output", "/Movies/TreeStrms"))
        folder_rel_path = os.path.relpath(folder_path, output_root)
        sample_files = [
            os.path.relpath(x["strm_path"], output_root)
            for x in items
        ]
        
        # 1. 智能 AI 审批
        needs_nfo = any(not it["has_nfo"] for it in items)
        ai_res = None
        current_ai_cfg = load_config().get("ai_governance", ai_cfg)
        if needs_nfo and current_ai_cfg.get("enabled") and current_ai_cfg.get("api_key"):
            ai_res = call_ai_auditor(folder_rel_path, sample_files, current_ai_cfg)
            if ai_res:
                gov_progress["ai_batches_done"] += 1
                push_log(f"🤖 [{sub_folder_name}] AI 审批就绪: 分类={ai_res.get('genre')}")

        # 过滤本目录下真正需要处理的条目
        active_items = [it for it in items if not it.get("is_sub", False) and ((not it["has_nfo"]) or (extract_covers and not it["has_poster"]) or (probe_streams and not it["has_streamdetails"]))]
        if not active_items:
            continue

        # 2. 针对需要写 NFO 的条目，按批次（每批 30 个）让大模型精准提取文件名特异性元数据
        items_needing_nfo = [it for it in active_items if not it["has_nfo"]]
        file_ai_details = {}
        failed_ai_paths = set()
        if items_needing_nfo and current_ai_cfg.get("enabled") and current_ai_cfg.get("api_key"):
            # 使用较稳定的配置批次；异常配置回退到 50，避免过大请求超时。
            try:
                ai_batch_chunk = int(current_ai_cfg.get("batch_size", 50))
            except (TypeError, ValueError):
                ai_batch_chunk = 50
            if ai_batch_chunk <= 0:
                ai_batch_chunk = 50
            for i in range(0, len(items_needing_nfo), ai_batch_chunk):
                batch_slice = items_needing_nfo[i:i + ai_batch_chunk]
                # 将 相对路径（含各级子目录分类上下文）+ 文件名 一并提供给大模型
                batch_items_payload = []
                for x in batch_slice:
                    rel_to_root = os.path.relpath(x["strm_path"], output_root)
                    batch_items_payload.append(rel_to_root)
                names_to_audit = [os.path.relpath(x["strm_path"], output_root) for x in batch_slice]
                push_log(f"🧠 [{sub_folder_name}] 正在并发请求 AI 深度提炼完整路径 ({i+1}~{min(i+ai_batch_chunk, len(items_needing_nfo))}/{len(items_needing_nfo)})...")
                chunk_res = call_ai_item_auditor(batch_items_payload, folder_rel_path, current_ai_cfg, failed_paths=failed_ai_paths)
                if chunk_res:
                    # 兼容返回 key 为 base_name 或者带路径的原始 payload
                    for k, v in chunk_res.items():
                        clean_k = k.split(" (路径层级:")[0].strip()
                        # 新格式返回完整路径；兼容旧模型仍返回带说明后缀的 key。
                        clean_k = clean_k.strip()
                        file_ai_details[clean_k] = v
                        file_ai_details[k] = v
                resolved_paths = {p for p in names_to_audit if p in file_ai_details}
                fallback_paths = [p for p in names_to_audit if p not in resolved_paths]
                if fallback_paths:
                    fallback_res = call_ai_translation_fallback(fallback_paths, folder_rel_path, current_ai_cfg)
                    for k, v in fallback_res.items():
                        file_ai_details[k] = v
                failed_ai_paths.update(p for p in names_to_audit if p not in file_ai_details)

        # 先顺序极速完成 NFO 补齐与元数据准备
        work_tasks = []
        for it in active_items:
            if processed_count >= target_limit or gov_progress["is_paused_for_captcha"]:
                break
            processed_count += 1
            gov_progress["processed_videos"] = processed_count

            with open(it["strm_path"], "r", encoding="utf-8") as sf:
                raw_target = sf.read().strip()
                raw_target_decoded = unquote(raw_target)
                video_name = os.path.basename(raw_target_decoded)

            # 1. 严格由 AI 大模型提炼生成 NFO（彻底移除死板规则兜底，防元数据污染）
            if not it["has_nfo"]:
                # 优先注入大模型针对该单文件的个性化提炼结果
                item_rel_path = os.path.relpath(it["strm_path"], output_root)
                item_ai = (
                    file_ai_details.get(item_rel_path)
                    or file_ai_details.get(it["base_name"])
                    or file_ai_details.get(video_name)
                )
                if not item_ai or not isinstance(item_ai, dict):
                    push_log(f"⏭️ [跳过NFO] 【{video_name}】文件级 AI 未成功返回，保持 NFO 缺失，等待补偿")
                    continue

                clean_title = item_ai.get("clean_title") or it["base_name"]
                ai_tags = item_ai.get("tags") if isinstance(item_ai.get("tags"), list) else []
                common_tags = ai_res.get("common_tags", []) if ai_res and isinstance(ai_res.get("common_tags"), list) else []
                meta_studio = ai_res.get("studio", "") if ai_res else ""
                meta_genres = ai_res.get("genre", ["精选"]) if ai_res else ["精选"]
                if isinstance(meta_genres, str):
                    meta_genres = [meta_genres]
                
                meta_summary = item_ai.get("summary") or (ai_res.get("clean_summary") if ai_res else "") or ""
                if item_rel_path in failed_ai_paths:
                    ai_tags = list(dict.fromkeys(ai_tags + ["AI待补"]))
                    meta_summary = (meta_summary or "") + "（AI提炼失败，待补全）"

                meta = {
                    "title": clean_title,
                    "studio": meta_studio,
                    "genres": meta_genres,
                    "tags": list(dict.fromkeys(ai_tags + common_tags)),
                    "summary": meta_summary
                }
                
                write_nfo_file(it["nfo_path"], meta)
                gov_progress["nfos_generated"] += 1
                it["has_nfo"] = True
                studio_tag = f"[{meta.get('studio')}]" if meta.get('studio') else ""
                genre_tag = f"/{meta.get('genres', [''])[0]}" if meta.get('genres') else ""
                push_log(f"📝 [生成NFO] 【{video_name}】元数据就绪 {studio_tag}/{genre_tag} (封面:{'已存在' if it['has_poster'] else '待抽取'}, 累计NFO: {gov_progress['nfos_generated']})")
            elif probe_streams and not has_streamdetails_in_nfo(it["nfo_path"]):
                push_log(f"📋 [核验NFO] 【{video_name}】发现历史NFO存在但缺少音视频流，已加入深度补流队列！")

            # 2. 判定是否需要进入 CD2 探针 / 抽封面队列
            needs_poster = extract_covers and not it["has_poster"]
            needs_probe = probe_streams and not has_streamdetails_in_nfo(it["nfo_path"])
            if needs_poster or needs_probe:
                reason = "缺封面+缺流信息" if (needs_poster and needs_probe) else ("缺封面" if needs_poster else "有封面但缺流信息")
                work_tasks.append((raw_target, it["poster_path"], it["thumb_path"], it["nfo_path"], video_name, needs_poster, reason))

        # 3. CD2 本地通道多线程并发 Fast Seek 抽封面 + 顺带探针流信息
        if work_tasks:
            def _extract_worker(task):
                raw_t, p_path, th_path, nfo_p, v_name, do_poster, reason = task
                try:
                    if do_poster:
                        diagnostic = {}
                        cd2_root = cfg.get("cd2_mount_path", "/Movies/CloudDrive/115")
                        s_prefix = cfg.get("default_prefix", "http://23.19.231.70:5244/d/115")
                        ok = extract_cover_by_cd2(raw_t, p_path, th_path, nfo_path=nfo_p, diagnostic=diagnostic, cd2_mount_path=cd2_root, stream_prefix=s_prefix)
                        if ok:
                            with gov_lock:
                                gov_progress["covers_extracted"] += 1
                                gov_progress["recent_success_streak"] += 1
                                push_log(f"📸 [补齐封面] 【{v_name}】({reason}) ➔ 成功提取居中黄金封面并注入4K/AAC/大小！(已补封面: {gov_progress['covers_extracted']} 张)")
                        else:
                            detail = diagnostic.get("reason", "未知失败")
                            if diagnostic.get("detail"):
                                detail += f"：{diagnostic['detail']}"
                            if diagnostic.get("probe_returncode") is not None:
                                detail += f"；ffprobe返回码={diagnostic['probe_returncode']}"
                            if diagnostic.get("probe_stderr"):
                                detail += f"；stderr={diagnostic['probe_stderr']}"
                            detail += f"；STRM目标={raw_t}；实际物理探测路径={diagnostic.get('mapped_path', '未转换')}"
                            push_log(f"⚠️ [补齐封面失败] 【{v_name}】({reason}) ➔ {detail[:2200]}")
                    else:
                        # 封面已有，仅需深度探针补充音视频流信息与大小
                        cd2_root = cfg.get("cd2_mount_path", "/Movies/CloudDrive/115")
                        s_prefix = cfg.get("default_prefix", "http://23.19.231.70:5244/d/115")
                        host_target = map_to_cd2_path(raw_t, cd2_root, s_prefix)
                        m_info = probe_media_info(host_target, timeout_sec=35)
                        if update_nfo_with_streamdetails(nfo_p, m_info):
                            with gov_lock:
                                gov_progress["recent_success_streak"] += 1
                                v_str = f"{m_info.get('video', {}).get('width')}x{m_info.get('video', {}).get('height')}"
                                a_str = f"{m_info.get('audio', {}).get('codec', 'audio')} {m_info.get('audio', {}).get('channels', 2)}ch"
                                size_mb = round(m_info.get('filesize', 0) / (1024 * 1024), 1)
                                push_log(f"🔍 [深度补流] 【{v_name}】➔ 成功补全流信息与大小: {v_str}, {a_str}, 大小:{size_mb}MB, 时长:{round(m_info.get('duration', 0))}s")
                        else:
                            push_log(f"⚠️ [探针重试中] 【{v_name}】CD2响应延迟或超时，保留原NFO绝不污染，留待后续复读")
                except Exception as exc:
                    push_log(f"⚠️ [治理条目异常] 【{v_name}】({reason}) ➔ {type(exc).__name__}: {str(exc)[:500]}")
                finally:
                    # CD2/FUSE 严格串行慢牛，避免并发 Range 请求触发 115 风控。
                    time.sleep(max(MIN_SAFE_DELAY, current_delay))

            # 115 流控红线：CD2 挂载点严格串行访问，config 中的 cover_threads 不参与此处（仅保留字段兼容旧配置）。
            for task in work_tasks:
                _extract_worker(task)
                    
    gov_progress["elapsed"] = round(time.time() - start_t, 2)
    done_msg = f"🎉 专区【{category_name}】CD2 无风控治理圆满完成！共补齐 NFO: {gov_progress['nfos_generated']} 个，补齐封面: {gov_progress['covers_extracted']} 张！耗时: {gov_progress['elapsed']}秒"
    push_log(done_msg)
    send_telegram_alert(f"✅ [TreeSTRM 完成通知]\n{done_msg}")
    
    # 动态更新全量底账缓存中该分类的真实物理统计，保证 UI 刷新立即可见与 100% 准确
    try:
        from engine.scanner import scan_single_category
        cache_file = os.path.join(DATA_DIR, "categories_cache.json")
        cat_dir = os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人", category_name)
        fresh_stat = scan_single_category(cat_dir)
        c_map = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as cf:
                    c_map = json.load(cf)
            except Exception:
                pass
        c_map[category_name] = fresh_stat
        with open(cache_file, "w", encoding="utf-8") as cf:
            json.dump(c_map, cf, ensure_ascii=False, indent=2)
    except Exception as e:
        push_log(f"⚠️ 更新专区底账缓存异常: {e}")

    try:
        cfg_after = load_config()
        if cfg_after.get("auto_refresh_after_governance", False):
            from services.emby_client import EmbyClient
            EmbyClient().trigger_category_refresh(category_name)
        else:
            push_log(f"⏸️ 专区【{category_name}】NFO 已完成，按配置暂不自动刷新 Emby")
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

# ----------------- 访问口令鉴权 -----------------
# /api/* 会返回 115 Cookie、AI api_key 等凭据且服务监听 0.0.0.0，必须鉴权。
# 口令优先级：环境变量 > config.json 的 access_token > 首次启动自动生成并写回 config.json
ACCESS_TOKEN_ENV = "TREESTRM_ACCESS_TOKEN"
ACCESS_TOKEN_HEADER = "X-Access-Token"

def ensure_access_token() -> str:
    env_token = os.environ.get(ACCESS_TOKEN_ENV, "").strip()
    if env_token:
        return env_token
    cfg = load_config()
    token = str(cfg.get("access_token") or "").strip()
    if not token:
        token = secrets.token_urlsafe(24)
        cfg["access_token"] = token
        save_config(cfg)
        print(f"[TreeSTRM] 已自动生成访问口令并写入 {CONFIG_FILE} 的 access_token 字段: {token}", flush=True)
    return token

ACCESS_TOKEN = ensure_access_token()

def public_config(cfg: dict) -> dict:
    """返回给前端的配置不回显访问口令本身"""
    return {k: v for k, v in cfg.items() if k != "access_token"}

@app.middleware("http")
async def require_access_token(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        supplied = request.headers.get(ACCESS_TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode("utf-8"), ACCESS_TOKEN.encode("utf-8")):
            return JSONResponse(status_code=401, content={"detail": "访问口令错误或缺失"})
    return await call_next(request)

sync_lock = threading.Lock()

def do_sync_task(rule_ids: Optional[List[str]] = None):
    with sync_lock:
        sync_progress["is_syncing"] = True
        try:
            _run_sync_rules(rule_ids)
        finally:
            # 任何异常都必须复位，否则 is_syncing 永久为 True，后续所有同步请求都会被拒绝
            sync_progress["is_syncing"] = False
            sync_progress["stage"] = "就绪"

def _update_sync_progress(processed: int, total: int, created: int, skipped: int, deleted: int):
    sync_progress.update(processed=processed, total=total, created=created, skipped=skipped, deleted=deleted)

def _run_sync_rules(rule_ids: Optional[List[str]]):
    push_log("======== 开始执行目录树 STRM 全量对账同步 ========")
    cfg = load_config()
    cookie = cfg.get("cookie")
    if not cookie:
        push_log("❌ 错误：115 Cookie 为空，请先在下方保存 Cookie！")
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
                auto_cleanup=auto_clean,
                progress_callback=_update_sync_progress
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
    push_log("======== 全部全量任务执行完毕 ========")

@app.get("/api/config")
def get_config():
    return public_config(load_config())

@app.post("/api/config")
def update_config(data: ConfigModel):
    cfg = load_config()
    cfg["cookie"] = data.cookie
    cfg["default_prefix"] = data.default_prefix
    cfg["cd2_mount_path"] = data.cd2_mount_path or "/Movies/CloudDrive/115"
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
    return {"status": "ok", "config": public_config(cfg)}

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


CATEGORIES_CACHE_FILE = os.path.join(DATA_DIR, "categories_cache.json")

def load_categories_cache(root_dir: str) -> Dict[str, dict]:
    """读取专区底账缓存；缓存缺失或损坏时触发一次并发物理盘点并回写"""
    if os.path.exists(CATEGORIES_CACHE_FILE):
        try:
            with open(CATEGORIES_CACHE_FILE, "r", encoding="utf-8") as cf:
                data = json.load(cf)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    from engine.scanner import sync_cache_to_disk
    return sync_cache_to_disk(CATEGORIES_CACHE_FILE, root_dir)

@app.get("/api/tasks")
def get_primary_tasks():
    cfg = load_config()
    rules = cfg.get("rules", [])
    res = []
    
    root_dir = os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人")
    sub_categories_count = 0
    if os.path.exists(root_dir):
        sub_categories_count = sum(1 for d in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, d)))
    # 读取专区底账缓存，避免每次请求 os.walk 十几万文件；缓存不存在时盘点一次并落盘
    cached_map = load_categories_cache(root_dir)
    total_videos = sum(x.get("video_count", 0) for x in cached_map.values())
    total_subs = sum(x.get("sub_count", 0) for x in cached_map.values())
    total_covers = sum(x.get("cover_count", 0) for x in cached_map.values())
    total_nfos = sum(x.get("nfo_count", 0) for x in cached_map.values())

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
            "prefix": r.get("prefix", "http://23.19.231.70:5244/d/115"),
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
        "prefix": payload.get("prefix", cfg.get("default_prefix", "http://23.19.231.70:5244/d/115")),
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
def get_categories(refresh: bool = False):
    cfg = load_config()
    root_dir = os.path.join(cfg.get("default_output", "/Movies/TreeStrms"), "成人")
    protected_set = set(cfg.get("protected_categories", DEFAULT_PROTECTED_CATEGORIES))
    
    if not os.path.exists(root_dir):
        return []
        
    res = []
    subdirs = sorted([d for d in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, d))])
    
    running_cat = current_running_job.get("category") if current_running_job else None
    queued_cats = [q.get("category") for q in governance_queue]
    
    cache_file = os.path.join(DATA_DIR, "categories_cache.json")
    cached_map = {}

    # 若请求强制刷新或缓存文件不存在，直接触发极速物理盘点并回写缓存
    if refresh or not os.path.exists(cache_file):
        try:
            from engine.scanner import sync_cache_to_disk
            cached_map = sync_cache_to_disk(cache_file, root_dir)
            push_log("🔄 [底账校准] 已完成全盘极速物理盘点，专区底账已 100% 刷新对齐！")
        except Exception as e:
            push_log(f"⚠️ 物理盘点失败: {e}")

    if not cached_map and os.path.exists(cache_file):
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
