"""刮削服务：任务队列（单线程顺序执行，重启续跑）、单条处理、人工修正"""

from __future__ import annotations

import os
import threading
import time
import traceback
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .http import Fetcher
from .identify import identify, info_from_override
from .merge import collect, from_candidate, score
from .models import KIND_LABELS, MediaInfo, Metadata, ScrapeOutcome
from .output import OVERWRITE_ALL, OVERWRITE_MISSING, OVERWRITE_POLICIES, plan, write_outcome
from .sources import Registry
from .sources.base import Context
from .store import Store
from .translate import TRANSLATE_MODES, TRANSLATE_OFF, translate

ADULT_DIR = "成人"
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".smi", ".lrc"}

DEFAULTS: Dict[str, Any] = {
    "sources": {},             # {站点名: 是否启用}，未列出的按站点默认值
    "min_score": 0.6,          # 低于该置信度不落盘，进入「待确认」
    "overwrite": OVERWRITE_MISSING,
    "write_fanart": True,      # 写 -fanart.jpg / -thumb.jpg
    "crop_poster": True,       # 日式横版封套裁出竖版海报
    "title_with_number": False,
    "translate": TRANSLATE_OFF,
    "request_interval": 1.5,   # 同一域名最小请求间隔（秒）
    "cache_days": 7,           # 页面缓存天数，0 为不缓存
    "retry_failed_days": 3,    # 失败条目多少天后才自动重试
    "max_sources": 3,          # 每条最多从几个站点取到有效结果后停止
}


def _float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def normalize_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (raw or {}).items() if k in DEFAULTS})
    s["sources"] = {str(k): bool(v) for k, v in (s.get("sources") or {}).items()} if isinstance(s.get("sources"), dict) else {}
    s["min_score"] = _float(s["min_score"], 0.6, 0.3, 1.0)
    s["overwrite"] = s["overwrite"] if s["overwrite"] in OVERWRITE_POLICIES else OVERWRITE_MISSING
    s["translate"] = s["translate"] if s["translate"] in TRANSLATE_MODES else TRANSLATE_OFF
    s["request_interval"] = _float(s["request_interval"], 1.5, 0.3, 30)
    s["cache_days"] = _float(s["cache_days"], 7, 0, 90)
    s["retry_failed_days"] = _float(s["retry_failed_days"], 3, 0, 365)
    s["max_sources"] = int(_float(s["max_sources"], 3, 1, 6))
    for key in ("write_fanart", "crop_poster", "title_with_number"):
        s[key] = bool(s[key])
    return s


def is_subtitle_strm(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            target = f.readline().strip()
        ext = os.path.splitext(target.split("?", 1)[0])[1].lower()
        if ext:
            return ext in SUBTITLE_EXTS
    except OSError:
        pass
    name = os.path.basename(path)[:-5]
    return any(name.lower().endswith(f"({e[1:]})") for e in SUBTITLE_EXTS)


class PathError(ValueError):
    pass


class ScraperService:
    def __init__(self, *, data_dir: str, load_config: Callable[[], Dict[str, Any]], log: Callable[[str], None],
                 on_category_done: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self._load_config = load_config
        self._cfg_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
        self.log = log
        self.on_category_done = on_category_done
        root = os.path.join(data_dir, "scraper")
        os.makedirs(root, exist_ok=True)
        self.fetcher = Fetcher(os.path.join(root, "cache"))
        self.registry = Registry(Context(self.fetcher, self.load_config))
        self.store = Store(os.path.join(root, "scraper.db"))
        self._queue: Deque[int] = deque()
        self._cond = threading.Condition()
        self._cancel = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self.progress: Dict[str, Any] = self._idle_progress()

    # ---------- 配置与路径 ----------
    def load_config(self) -> Dict[str, Any]:
        """每条目会多次读取配置（站点开关等），短时缓存避免反复读盘"""
        at, cfg = self._cfg_cache
        if time.monotonic() - at > 3:
            cfg = self._load_config()
            self._cfg_cache = (time.monotonic(), cfg)
        return cfg

    def invalidate_config(self) -> None:
        self._cfg_cache = (0.0, {})

    def settings(self) -> Dict[str, Any]:
        s = normalize_settings(self.load_config().get("scraper") or {})
        self.fetcher.configure(min_interval_s=s["request_interval"], cache_ttl_s=s["cache_days"] * 86400)
        return s

    def adult_root(self) -> str:
        return os.path.abspath(os.path.join(self.load_config().get("default_output", "/Movies/TreeStrms"), ADULT_DIR))

    def resolve(self, rel_path: str) -> str:
        """条目路径（相对「成人」目录）→ 绝对路径，拒绝越界与非 .strm"""
        root = self.adult_root()
        rel = (rel_path or "").replace("\\", "/").lstrip("/")
        full = os.path.abspath(os.path.join(root, rel))
        if os.path.commonpath([root, full]) != root or not full.lower().endswith(".strm"):
            raise PathError("非法条目路径")
        if not os.path.isfile(full):
            raise PathError("条目文件不存在")
        return full

    def rel(self, full: str) -> str:
        return os.path.relpath(full, self.adult_root()).replace(os.sep, "/")

    def identify_path(self, rel_path: str) -> MediaInfo:
        parts = rel_path.split("/")
        filename = parts[-1]
        parent = parts[-2] if len(parts) >= 2 else ""
        return identify(filename, parent, "/".join(parts[:-1]))

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        for job in self.store.pending_jobs():
            if job["status"] == "running":
                self.store.update_job(job["id"], status="queued", message="服务重启后续跑")
            self._queue.append(job["id"])
        if self._queue:
            self.log(f"🎬 [刮削] 恢复 {len(self._queue)} 个未完成的刮削任务")
        self._worker = threading.Thread(target=self._loop, name="scraper-worker", daemon=True)
        self._worker.start()

    def enqueue(self, category: str, options: Dict[str, Any]) -> int:
        cat_dir = os.path.join(self.adult_root(), category)
        if not category or category in (".", "..") or os.path.basename(category) != category or "\\" in category or not os.path.isdir(cat_dir):
            raise PathError("专区路径不存在")
        opts = {
            "overwrite": options.get("overwrite") if options.get("overwrite") in OVERWRITE_POLICIES else self.settings()["overwrite"],
            "max_items": max(0, int(options.get("max_items") or 0)),
            "retry_failed": bool(options.get("retry_failed")),
        }
        job_id = self.store.add_job(category, opts)
        with self._cond:
            self._queue.append(job_id)
            self._cond.notify()
        self.log(f"🎬 [刮削] 专区【{category}】已加入刮削队列（覆盖策略: {opts['overwrite']}）")
        return job_id

    def cancel(self, clear_queue: bool = False) -> Dict[str, int]:
        cleared = 0
        with self._cond:
            if clear_queue:
                while self._queue:
                    self.store.update_job(self._queue.popleft(), status="cancelled", finished_at=time.time())
                    cleared += 1
        if self.progress.get("running"):
            self._cancel.set()
        return {"cleared": cleared}

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                job_id = self._queue.popleft()
            job = next((j for j in self.store.pending_jobs() if j["id"] == job_id), None)
            if not job:
                continue
            self._cancel.clear()
            try:
                self._run_job(job)
            except Exception as exc:
                self.store.update_job(job_id, status="error", message=f"{type(exc).__name__}: {exc}"[:500], finished_at=time.time())
                self.log(f"❌ [刮削] 任务异常: {type(exc).__name__}: {exc}")
                traceback.print_exc()
            finally:
                self.progress = self._idle_progress()

    @staticmethod
    def _idle_progress() -> Dict[str, Any]:
        return {"running": False, "job_id": None, "category": "", "stage": "空闲", "total": 0, "done": 0,
                "matched": 0, "low_confidence": 0, "not_found": 0, "error": 0, "skipped": 0,
                "nfo": 0, "poster": 0, "current": "", "started_at": 0, "cancelling": False}

    # ---------- 任务 ----------
    def _scan(self, category: str, policy: str, retry_failed: bool, settings: Dict[str, Any]) -> Tuple[List[Tuple[str, Dict[str, bool]]], int]:
        retry_days = settings["retry_failed_days"]
        cat_dir = os.path.join(self.adult_root(), category)
        found: List[Tuple[str, Dict[str, bool]]] = []
        complete = 0
        for root, dirs, files in os.walk(cat_dir):
            dirs.sort()
            for name in sorted(files):
                if not name.lower().endswith(".strm"):
                    continue
                full = os.path.join(root, name)
                todo = plan(full, policy, settings["write_fanart"])
                if not any(todo.values()):
                    complete += 1
                    continue
                found.append((full, todo))
        # 读取状态库过滤：忽略的不处理，近期失败的不重复请求
        records = self.store.get_many(self.rel(f) for f, _ in found)
        cutoff = time.time() - retry_days * 86400
        out = []
        for full, todo in found:
            rec = records.get(self.rel(full))
            if rec:
                if rec["status"] == "ignored":
                    continue
                recent = rec["updated_at"] > cutoff and not retry_failed
                if rec["status"] != "matched" and not rec.get("override") and recent:
                    continue
                # 已匹配但图片下载失败的条目同样按重试间隔再试，NFO 被删除的立即重刮
                if rec["status"] == "matched" and not todo["nfo"] and recent:
                    continue
            if is_subtitle_strm(full):
                continue
            out.append((full, todo))
        return out, complete

    def _run_job(self, job: Dict[str, Any]) -> None:
        category, opts = job["category"], job["options"]
        settings = self.settings()
        policy = opts.get("overwrite") or settings["overwrite"]
        self.store.update_job(job["id"], status="running", started_at=time.time())
        p = self._idle_progress()
        p.update(running=True, job_id=job["id"], category=category, stage="扫描专区", started_at=time.time())
        self.progress = p
        self.log(f"======== 🎬 [刮削] 开始专区【{category}】（覆盖策略: {policy}） ========")

        targets, complete = self._scan(category, policy, bool(opts.get("retry_failed")), settings)
        max_items = int(opts.get("max_items") or 0)
        if max_items > 0:
            targets = targets[:max_items]
        p.update(total=len(targets), skipped=complete, stage="刮削中")
        self.log(f"🎬 [刮削] 专区【{category}】待刮削 {len(targets)} 部，已完整 {complete} 部")

        for full, todo in targets:
            if self._cancel.is_set():
                break
            rel = self.rel(full)
            p["current"] = rel
            try:
                outcome, written = self.process(rel, settings=settings, policy=policy, todo=todo)
                p[outcome.status] = p.get(outcome.status, 0) + 1
                p["nfo"] += int(written.get("nfo", False))
                p["poster"] += int(written.get("poster", False))
                self._log_outcome(rel, outcome, written)
            except Exception as exc:
                p["error"] += 1
                self.store.upsert(rel, category=category, status="error", message=f"{type(exc).__name__}: {exc}"[:500])
                self.log(f"⚠️ [刮削异常] {rel}: {type(exc).__name__}: {exc}")
            p["done"] += 1

        cancelled = self._cancel.is_set()
        stats = {k: p[k] for k in ("total", "done", "matched", "low_confidence", "not_found", "error", "skipped", "nfo", "poster")}
        self.store.update_job(job["id"], status="cancelled" if cancelled else "done", stats=stats, finished_at=time.time())
        elapsed = int(time.time() - p["started_at"])
        self.log(
            f"🎉 [刮削] 专区【{category}】{'已取消' if cancelled else '完成'}：成功 {p['matched']}，待确认 {p['low_confidence']}，"
            f"未找到 {p['not_found']}，出错 {p['error']}；写入 NFO {p['nfo']} 个、海报 {p['poster']} 张，耗时 {elapsed}s"
        )
        if self.on_category_done and (p["nfo"] or p["poster"]):
            try:
                self.on_category_done(category, stats)
            except Exception as exc:
                self.log(f"⚠️ [刮削] 完成回调异常: {exc}")

    def _log_outcome(self, rel: str, outcome: ScrapeOutcome, written: Dict[str, bool]) -> None:
        name = rel.rsplit("/", 1)[-1]
        if outcome.status == "matched" and outcome.meta:
            parts = [k for k, v in written.items() if v]
            self.log(f"📝 [刮削] {name} ➔ {outcome.meta.number or ''} {outcome.meta.title[:40]}"
                     f"（{'+'.join(outcome.sources_used)}，写入: {','.join(parts) or '无'}）")
        elif outcome.status == "low_confidence":
            best = outcome.candidates[0] if outcome.candidates else None
            self.log(f"🤔 [待确认] {name}：最佳候选 {best.source if best else ''} 置信度 {best.score if best else 0:.2f}")
        else:
            self.log(f"⏭️ [{'未找到' if outcome.status == 'not_found' else '出错'}] {name}：{outcome.message[:200]}")

    # ---------- 单条处理 ----------
    def _download(self, url: str, referer: str) -> Optional[bytes]:
        return self.fetcher.download_image(url, referer=referer)

    def _finish(self, rel: str, full: str, outcome: ScrapeOutcome, settings: Dict[str, Any], policy: str,
                todo: Optional[Dict[str, bool]] = None, override: Any = None) -> Dict[str, bool]:
        written: Dict[str, bool] = {}
        if outcome.status == "matched" and outcome.meta:
            if settings["translate"] != TRANSLATE_OFF:
                translate(outcome.meta, settings["translate"], self.load_config().get("ai_governance") or {})
            written = write_outcome(
                full, outcome, policy=policy, download=self._download, todo=todo,
                title_with_number=settings["title_with_number"], crop_poster=settings["crop_poster"],
                write_fanart=settings["write_fanart"],
            )
        meta = outcome.meta
        fields: Dict[str, Any] = dict(
            category=rel.split("/", 1)[0],
            status=outcome.status,
            kind=outcome.info.kind,
            number=(meta.number if meta else outcome.info.number) or "",
            source="+".join(outcome.sources_used) if meta else "",
            score=meta.score if meta else (outcome.candidates[0].score if outcome.candidates else 0),
            title=meta.title if meta else "",
            message=outcome.message,
            info=outcome.info.to_dict(),
            candidates=[c.to_dict() for c in outcome.candidates[:10]],
        )
        if override is not None:
            fields["override"] = override
        self.store.upsert(rel, **fields)
        return written

    def process(self, rel: str, *, settings: Optional[Dict[str, Any]] = None, policy: str = "",
                todo: Optional[Dict[str, bool]] = None) -> Tuple[ScrapeOutcome, Dict[str, bool]]:
        settings = settings or self.settings()
        full = self.resolve(rel)
        info = self.identify_path(rel)
        rec = self.store.get(rel)
        override = (rec or {}).get("override") or None
        if isinstance(override, dict) and override.get("candidate"):
            outcome = from_candidate(self.registry, info, Metadata.from_dict(override["candidate"]), max_sources=settings["max_sources"])
        else:
            if isinstance(override, dict) and override.get("number"):
                info = info_from_override(override["number"], info)
            outcome = collect(self.registry, info, min_score=settings["min_score"], max_sources=settings["max_sources"])
        written = self._finish(rel, full, outcome, settings, policy or settings["overwrite"], todo)
        return outcome, written

    # ---------- 人工修正 ----------
    def search(self, rel: str, query: str = "", source: str = "") -> Dict[str, Any]:
        self.resolve(rel)
        settings = self.settings()
        info = self.identify_path(rel)
        query = (query or "").strip()
        results: List[Metadata] = []
        errors: List[str] = []
        target = info_from_override(query, info) if query else info
        sources = [self.registry.sources[source]] if source in self.registry.sources else None
        if sources is None:
            outcome = collect(self.registry, target, min_score=2.0, max_sources=6)  # min_score>1：只收集候选，不提前停止
            results = outcome.candidates
            # 搜索模式下候选必然“置信度不足”，只透出站点错误
            if outcome.message and outcome.status != "low_confidence":
                errors.append(outcome.message)
            elif outcome.status == "low_confidence" and ": " in outcome.message:
                errors.append(outcome.message)
            # 番号查不到时再用关键词全文检索（欧美 / 里番 / 自由文本）
            if query and not results:
                for name in ("tpdb", "javdb", "getchu"):
                    if self.registry.enabled(name):
                        try:
                            results += self.registry.sources[name].search(query, target)
                        except Exception as exc:
                            errors.append(f"{name}: {type(exc).__name__}")
        else:
            try:
                results = sources[0].search(query, target) if query else sources[0].fetch(target)
            except Exception as exc:
                errors.append(f"{source}: {type(exc).__name__}: {exc}"[:200])
        for m in results:
            m.score = score(target, m) if m.score <= 0 else m.score
        results.sort(key=lambda m: m.score, reverse=True)
        return {
            "info": info.to_dict(),
            "kind_label": KIND_LABELS.get(info.kind, info.kind),
            "query_info": target.to_dict(),
            "candidates": [m.to_dict() for m in results[:20]],
            "message": "；".join(errors),
            "min_score": settings["min_score"],
        }

    def apply(self, rel: str, *, candidate: Optional[Dict[str, Any]] = None, number: str = "",
              overwrite: str = OVERWRITE_ALL) -> Dict[str, Any]:
        """人工指定：选定候选或改番号重刮；结果按指定策略写入（默认覆盖），并记住修正以便日后重跑"""
        full = self.resolve(rel)
        settings = self.settings()
        info = self.identify_path(rel)
        if candidate:
            meta = Metadata.from_dict(candidate)
            outcome = from_candidate(self.registry, info, meta, max_sources=settings["max_sources"])
            override: Dict[str, Any] = {"candidate": meta.to_dict()}
        elif number.strip():
            target = info_from_override(number.strip(), info)
            outcome = collect(self.registry, target, min_score=settings["min_score"], max_sources=settings["max_sources"])
            override = {"number": number.strip()}
        else:
            raise ValueError("需要指定候选或番号")
        policy = overwrite if overwrite in OVERWRITE_POLICIES else OVERWRITE_ALL
        written = self._finish(rel, full, outcome, settings, policy, override=override)
        if written.get("nfo") or written.get("poster"):
            self.log(f"✍️ [人工修正] {rel} ➔ {outcome.meta.number if outcome.meta else ''} {outcome.meta.title[:40] if outcome.meta else ''}")
        return {
            "status": outcome.status,
            "message": outcome.message,
            "written": written,
            "meta": outcome.meta.to_dict() if outcome.meta else None,
            "candidates": [c.to_dict() for c in outcome.candidates[:10]],
        }

    def ignore(self, rels: List[str], ignored: bool = True) -> int:
        count = 0
        for rel in rels:
            if ignored:
                info = self.identify_path(rel)
                self.store.upsert(rel, category=rel.split("/", 1)[0], status="ignored", kind=info.kind, number=info.number, message="已手动忽略")
            else:
                self.store.delete([rel])
            count += 1
        return count

    # ---------- 状态 ----------
    def brief(self) -> Dict[str, Any]:
        """供 /api/status 高频轮询：只返回内存里的进度，不查库"""
        with self._cond:
            queued = len(self._queue)
        return {"progress": dict(self.progress), "queued": queued}

    def status(self) -> Dict[str, Any]:
        with self._cond:
            queued = list(self._queue)
        jobs = self.store.recent_jobs(15)
        queue = [j for j in jobs if j["id"] in queued]
        p = dict(self.progress)
        p["cancelling"] = self._cancel.is_set() and p.get("running", False)
        if p.get("running") and p.get("done"):
            rate = (time.time() - p["started_at"]) / p["done"]
            p["eta_s"] = int(rate * max(0, p["total"] - p["done"]))
        return {
            "progress": p,
            "queue": [{"id": j["id"], "category": j["category"], "options": j["options"]} for j in queue],
            "recent_jobs": [j for j in jobs if j["status"] not in ("queued",)][:10],
            "counts": self.store.counts(),
            "sources": self.registry.describe(),
        }
