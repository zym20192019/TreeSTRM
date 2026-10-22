"""刮削状态库（SQLite）：每个条目的结论 / 候选 / 人工修正，以及任务队列（重启可续跑）"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

ITEM_STATUSES = ("matched", "low_confidence", "not_found", "error", "ignored")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    path TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    number TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    info TEXT NOT NULL DEFAULT '{}',
    candidates TEXT NOT NULL DEFAULT '[]',
    override TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, category);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    stats TEXT NOT NULL DEFAULT '{}',
    message TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
"""

_ITEM_FIELDS = ("category", "status", "kind", "number", "source", "score", "title", "message", "info", "candidates", "override")


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- items ----------
    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for key, empty in (("info", {}), ("candidates", []), ("override", None)):
            raw = d.get(key)
            try:
                d[key] = json.loads(raw) if raw else empty
            except ValueError:
                d[key] = empty
        return d

    def get(self, path: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE path=?", (path,)).fetchone()
        return self._row(row) if row else None

    def get_many(self, paths: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        paths = list(paths)
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for i in range(0, len(paths), 500):
                chunk = paths[i:i + 500]
                q = f"SELECT path, status, updated_at, override FROM items WHERE path IN ({','.join('?' * len(chunk))})"
                for row in self._conn.execute(q, chunk):
                    out[row["path"]] = dict(row)
        return out

    def upsert(self, path: str, **fields: Any) -> None:
        data = {k: v for k, v in fields.items() if k in _ITEM_FIELDS}
        for key in ("info", "candidates", "override"):
            if key in data and not isinstance(data[key], str):
                data[key] = json.dumps(data[key], ensure_ascii=False) if data[key] is not None else ""
        data["updated_at"] = time.time()
        cols = ["path"] + list(data)
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in data)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO items ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT(path) DO UPDATE SET {updates}",
                [path] + list(data.values()),
            )
            self._conn.commit()

    def set_status(self, paths: List[str], status: str, message: str = "") -> int:
        with self._lock:
            cur = self._conn.executemany(
                "UPDATE items SET status=?, message=?, updated_at=? WHERE path=?",
                [(status, message, time.time(), p) for p in paths],
            )
            self._conn.commit()
            return cur.rowcount

    def delete(self, paths: List[str]) -> None:
        with self._lock:
            self._conn.executemany("DELETE FROM items WHERE path=?", [(p,) for p in paths])
            self._conn.commit()

    def list(self, *, status: str = "", category: str = "", q: str = "", offset: int = 0, limit: int = 50) -> Tuple[List[Dict[str, Any]], int]:
        where, args = [], []
        if status:
            statuses = [s for s in status.split(",") if s in ITEM_STATUSES]
            if statuses:
                where.append(f"status IN ({','.join('?' * len(statuses))})")
                args += statuses
        if category:
            where.append("category=?")
            args.append(category)
        if q:
            where.append("(path LIKE ? OR number LIKE ? OR title LIKE ?)")
            args += [f"%{q}%"] * 3
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) FROM items {clause}", args).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM items {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                args + [max(1, min(500, limit)), max(0, offset)],
            ).fetchall()
        return [self._row(r) for r in rows], total

    def counts(self, category: str = "") -> Dict[str, int]:
        with self._lock:
            if category:
                rows = self._conn.execute("SELECT status, COUNT(*) c FROM items WHERE category=? GROUP BY status", (category,)).fetchall()
            else:
                rows = self._conn.execute("SELECT status, COUNT(*) c FROM items GROUP BY status").fetchall()
        out = {s: 0 for s in ITEM_STATUSES}
        out.update({r["status"]: r["c"] for r in rows})
        return out

    # ---------- jobs ----------
    def add_job(self, category: str, options: Dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (category, options, status, created_at) VALUES (?, ?, 'queued', ?)",
                (category, json.dumps(options, ensure_ascii=False), time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_job(self, job_id: int, **fields: Any) -> None:
        allowed = {"status", "stats", "message", "started_at", "finished_at"}
        data = {k: (json.dumps(v, ensure_ascii=False) if k == "stats" else v) for k, v in fields.items() if k in allowed}
        if not data:
            return
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {','.join(f'{k}=?' for k in data)} WHERE id=?", list(data.values()) + [job_id])
            self._conn.commit()

    def pending_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY id").fetchall()
        return [self._job(r) for r in rows]

    def recent_jobs(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._job(r) for r in rows]

    @staticmethod
    def _job(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for key in ("options", "stats"):
            try:
                d[key] = json.loads(d.get(key) or "{}")
            except ValueError:
                d[key] = {}
        return d
