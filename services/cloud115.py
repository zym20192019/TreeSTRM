#!/usr/bin/env python3
"""
TreeSTRM 115 官方云端服务交互模块
特性：
1. 会话与鉴权持久化 Cookie 管理
2. 目录树导出与 UTF-16LE 解码
3. 支持 300 项/批 batch_rename 官方极速改名通道
4. 子目录 CID 精准发现与缓存
"""

import time
import requests
from typing import Dict, Optional, List, Tuple
from core.events import push_log

OFFICIAL_115_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Origin': 'https://115.com',
    'Referer': 'https://115.com/'
}

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
    """向 115 官方发起目录树导出任务并下载解码"""
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
        
    push_log("正在携带认证 Cookie 从 115 CDN 拉取目录树...")
    resp = session.get(dl_url, timeout=90)
    
    if resp.status_code != 200 or len(resp.content) < 500:
        raise RuntimeError(f"目录树下载失败 (HTTP {resp.status_code}, 长度: {len(resp.content)} bytes)")
        
    mb_size = round(len(resp.content) / (1024 * 1024), 2)
    push_log(f"目录树全量下载成功！大小: {mb_size} MB")
    return resp.content.decode('utf-16le', errors='ignore')

def batch_rename_115_files(session: requests.Session, rename_dict: Dict[str, str], batch_size: int = 300) -> Tuple[int, int]:
    """
    115 官方极速批量重命名 (files_new_name)
    rename_dict: { "fid": "new_name" }
    返回: (成功数, 失败数)
    """
    if not rename_dict:
        return 0, 0
        
    items = list(rename_dict.items())
    success_cnt = 0
    fail_cnt = 0
    
    for i in range(0, len(items), batch_size):
        chunk = items[i:i + batch_size]
        payload = {}
        for fid, new_name in chunk:
            payload[f'files_new_name[{fid}]'] = new_name
            
        try:
            res = session.post("https://webapi.115.com/files/batch_rename", data=payload, timeout=20).json()
            if res.get('state'):
                success_cnt += len(chunk)
            else:
                fail_cnt += len(chunk)
                push_log(f"⚠️ 批量改名批次失败: {res.get('error') or res.get('msg')}")
        except Exception as e:
            fail_cnt += len(chunk)
            push_log(f"⚠️ 批量改名网络异常: {str(e)[:50]}")
            
        time.sleep(1.0) # 保持流控
        
    return success_cnt, fail_cnt
