#!/usr/bin/env python3
"""
TreeSTRM Emby 增量局部刷新联动客户端
特性：
1. 严禁全库全局刷新，仅调用局部路径与条目刷新
2. 遵守规范：Emby 多路径刷新使用 id={ItemId}&path={Path} 杜绝 500
3. 容器内外路径自动平滑映射 (/Movies/TreeStrms <-> /movies/TreeStrms)
4. 刮削完成后静默触发增量联动
"""

import requests
from typing import Optional, Dict, Any
from core.events import push_log

EMBY_BASE_URL = "http://127.0.0.1:8096/emby"
DEFAULT_API_KEY = "920d837111e34b13a847435daa6ce99a"

class EmbyClient:
    def __init__(self, base_url: str = EMBY_BASE_URL, api_key: str = DEFAULT_API_KEY):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.headers = {
            "X-Emby-Token": self.api_key,
            "Accept": "application/json"
        }

    def is_online(self) -> bool:
        try:
            r = requests.get(f"{self.base_url}/System/Info", headers=self.headers, timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def find_item_by_path(self, file_path: str) -> Optional[str]:
        """根据媒体路径在 Emby 中查询 ItemId"""
        try:
            # 兼容容器内小写 /movies
            container_path = file_path
            if container_path.startswith('/Movies/'):
                container_path = '/movies/' + container_path[8:]
                
            url = f"{self.base_url}/Items"
            params = {
                "Recursive": "true",
                "Fields": "Path",
                "Path": container_path
            }
            r = requests.get(url, headers=self.headers, params=params, timeout=5)
            if r.status_code == 200:
                data = r.json()
                items = data.get("Items", [])
                if items:
                    return items[0].get("Id")
        except Exception:
            pass
        return None

    def refresh_item_by_id(self, item_id: str, path: Optional[str] = None) -> bool:
        """精准刷新单个 Item，不触发全局扫库"""
        try:
            url = f"{self.base_url}/Items/{item_id}/Refresh"
            params = {
                "MetadataRefreshMode": "Default",
                "ImageRefreshMode": "Default",
                "ReplaceAllMetadata": "false",
                "ReplaceAllImages": "false"
            }
            if path:
                params["path"] = path
            r = requests.post(url, headers=self.headers, params=params, timeout=5)
            return r.status_code in [200, 204]
        except Exception:
            return False

    def trigger_category_refresh(self, category_name: str) -> bool:
        """当专区刮削完成后，仅刷新该专区在 Emby 内部的挂载路径"""
        cat_path = f"/movies/TreeStrms/成人/{category_name}"
        push_log(f"🔄 正在通知 Emby 局部刷新专区【{category_name}】: {cat_path}")
        
        item_id = self.find_item_by_path(cat_path)
        if item_id:
            ok = self.refresh_item_by_id(item_id, cat_path)
            if ok:
                push_log(f"✅ Emby 局部刷新专区【{category_name}】指令已送达 (ItemId: {item_id})")
                return True
        push_log(f"ℹ️ Emby 中未直接定位到 {cat_path} 独立容器节点，等待下一次标准扫描")
        return False
