#!/usr/bin/env python3
"""
TreeSTRM 工业级微步任务状态仓 (SQLite WAL Checkpointing)
特性：
1. 采用 SQLite WAL 模式与 NORMAL 同步，兼顾高并发与极高崩溃安全性
2. 任务级 (Tasks) 与 条目级 (TaskItems) 双层状态机
3. 支持进程崩溃/重启后的毫秒级游标恢复与断点自动续跑
4. 专区统计数据增量快照缓存 (避免频繁全盘 I/O 扫盘)
"""

import os
import sqlite3
import json
import time
from typing import Optional, List, Dict, Any

DB_PATH = "/opt/treestrm/data/treestrm.db"

def get_db_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str = DB_PATH):
    """初始化状态仓数据表结构"""
    with get_db_connection(db_path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,       -- 'strm_sync', 'official_scrape', 'extract_covers', 'clean_orphan'
            category TEXT NOT NULL,        -- 专区名称，如 '成人', 'FC2', '欧美刮削'
            status TEXT NOT NULL,          -- 'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'PAUSED'
            total INTEGER DEFAULT 0,
            processed INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            metadata_json TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS task_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            media_path TEXT NOT NULL,
            base_name TEXT NOT NULL,
            status TEXT NOT NULL,          -- 'PENDING', 'SUCCESS', 'SKIPPED', 'FAILED'
            stage TEXT NOT NULL,           -- 'DISCOVERED', 'STRM_OK', 'NFO_OK', 'COVER_OK'
            error_msg TEXT,
            updated_at REAL NOT NULL,
            UNIQUE(task_id, media_path)
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_items_task ON task_items(task_id, status);

        CREATE TABLE IF NOT EXISTS category_cache (
            category_name TEXT PRIMARY KEY,
            video_count INTEGER DEFAULT 0,
            cover_count INTEGER DEFAULT 0,
            nfo_count INTEGER DEFAULT 0,
            is_protected INTEGER DEFAULT 0,
            last_checked REAL NOT NULL,
            stats_json TEXT DEFAULT '{}'
        );
        """)

class StateManager:
    """任务与状态微步跟踪器"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_db(self.db_path)

    def create_task(self, task_id: str, task_type: str, category: str, total: int = 0, metadata: Optional[Dict] = None) -> Dict[str, Any]:
        now = time.time()
        meta_str = json.dumps(metadata or {}, ensure_ascii=False)
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks (task_id, task_type, category, status, total, processed, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, 'PENDING', ?, 0, ?, ?, ?)
                """,
                (task_id, task_type, category, total, now, now, meta_str)
            )
        return {
            "task_id": task_id, "task_type": task_type, "category": category,
            "status": "PENDING", "total": total, "processed": 0
        }

    def start_task(self, task_id: str):
        now = time.time()
        with get_db_connection(self.db_path) as conn:
            conn.execute("UPDATE tasks SET status = 'RUNNING', updated_at = ? WHERE task_id = ?", (now, task_id))

    def update_progress(self, task_id: str, processed: int, success_inc: int = 0, failed_inc: int = 0):
        now = time.time()
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE tasks 
                SET processed = ?, 
                    success_count = success_count + ?, 
                    failed_count = failed_count + ?,
                    updated_at = ? 
                WHERE task_id = ?
                """,
                (processed, success_inc, failed_inc, now, task_id)
            )

    def record_item_step(self, task_id: str, media_path: str, base_name: str, status: str, stage: str, error: Optional[str] = None):
        now = time.time()
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO task_items (task_id, media_path, base_name, status, stage, error_msg, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, media_path) DO UPDATE SET
                    status = excluded.status,
                    stage = excluded.stage,
                    error_msg = excluded.error_msg,
                    updated_at = excluded.updated_at
                """,
                (task_id, media_path, base_name, status, stage, error, now)
            )

    def finish_task(self, task_id: str, status: str = "COMPLETED"):
        now = time.time()
        with get_db_connection(self.db_path) as conn:
            conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?", (status, now, task_id))

    def get_active_task(self) -> Optional[Dict[str, Any]]:
        with get_db_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE status = 'RUNNING' ORDER BY updated_at DESC LIMIT 1").fetchone()
            if row:
                return dict(row)
        return None

    def get_pending_tasks(self) -> List[Dict[str, Any]]:
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE status = 'PENDING' ORDER BY created_at ASC").fetchall()
            return [dict(r) for r in rows]

    def get_uncompleted_item_paths(self, task_id: str) -> set:
        """获取某任务中已处理成功的路径集合，用于断点续跑跳过"""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute("SELECT media_path FROM task_items WHERE task_id = ? AND status IN ('SUCCESS', 'SKIPPED')", (task_id,)).fetchall()
            return {r['media_path'] for r in rows}

    def recover_interrupted_jobs(self) -> List[Dict[str, Any]]:
        """服务重启时发现未完成的异常任务，重置为 PENDING 准备自愈续跑"""
        recovered = []
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE status = 'RUNNING'").fetchall()
            for r in rows:
                recovered.append(dict(r))
                conn.execute("UPDATE tasks SET status = 'PENDING', updated_at = ? WHERE task_id = ?", (time.time(), r['task_id']))
        return recovered
