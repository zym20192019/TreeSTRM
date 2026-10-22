"""刮削模块 HTTP 接口（/api/scraper/*），由 app.py include_router 挂载"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .output import OVERWRITE_POLICIES
from .runner import DEFAULTS, PathError, ScraperService, normalize_settings
from .sources import SOURCE_CLASSES
from .translate import TRANSLATE_MODES


class JobRequest(BaseModel):
    category: str
    overwrite: Optional[str] = None
    max_items: int = 0
    retry_failed: bool = False


class CancelRequest(BaseModel):
    clear_queue: bool = False


class SearchRequest(BaseModel):
    path: str
    query: str = ""
    source: str = ""


class ApplyRequest(BaseModel):
    path: str
    candidate: Optional[Dict[str, Any]] = None
    number: str = ""
    overwrite: str = "all"


class IgnoreRequest(BaseModel):
    paths: List[str]
    ignored: bool = True


class RetryRequest(BaseModel):
    path: str


class SettingsRequest(BaseModel):
    settings: Dict[str, Any]


def build_router(get_service: Callable[[], ScraperService], modify_config: Callable[[Callable[[dict], None]], dict]) -> APIRouter:
    router = APIRouter(prefix="/api/scraper", tags=["scraper"])

    def _guard(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except PathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.post("/jobs")
    def create_job(req: JobRequest):
        svc = get_service()
        job_id = _guard(lambda: svc.enqueue(req.category, req.model_dump() if hasattr(req, "model_dump") else req.dict()))
        return {"status": "queued", "job_id": job_id, "message": f"专区【{req.category}】已加入刮削队列"}

    @router.get("/status")
    def status():
        return get_service().status()

    @router.post("/cancel")
    def cancel(req: CancelRequest):
        res = get_service().cancel(req.clear_queue)
        return {"status": "ok", **res}

    @router.get("/items")
    def items(status: str = "", category: str = "", q: str = "", offset: int = 0, limit: int = 50):
        svc = get_service()
        rows, total = svc.store.list(status=status, category=category, q=q, offset=offset, limit=limit)
        for r in rows:
            r["candidates"] = r["candidates"][:3]
        return {"items": rows, "total": total, "counts": svc.store.counts(category)}

    @router.get("/item")
    def item(path: str):
        svc = get_service()
        rec = svc.store.get(path)
        info = _guard(lambda: (svc.resolve(path), svc.identify_path(path))[1])
        return {"record": rec, "info": info.to_dict()}

    @router.post("/search")
    def search(req: SearchRequest):
        return _guard(lambda: get_service().search(req.path, req.query, req.source))

    @router.post("/apply")
    def apply(req: ApplyRequest):
        return _guard(lambda: get_service().apply(req.path, candidate=req.candidate, number=req.number, overwrite=req.overwrite))

    @router.post("/retry")
    def retry(req: RetryRequest):
        svc = get_service()

        def run():
            outcome, written = svc.process(req.path)
            return {"status": outcome.status, "message": outcome.message, "written": written,
                    "meta": outcome.meta.to_dict() if outcome.meta else None,
                    "candidates": [c.to_dict() for c in outcome.candidates[:10]]}
        return _guard(run)

    @router.post("/items/ignore")
    def ignore(req: IgnoreRequest):
        svc = get_service()
        for p in req.paths:
            _guard(lambda p=p: svc.resolve(p))
        return {"status": "ok", "count": svc.ignore(req.paths, req.ignored)}

    @router.get("/settings")
    def get_settings():
        svc = get_service()
        return {
            "settings": svc.settings(),
            "defaults": DEFAULTS,
            "overwrite_policies": list(OVERWRITE_POLICIES),
            "translate_modes": list(TRANSLATE_MODES),
            "sources": svc.registry.describe(),
        }

    @router.post("/settings")
    def save_settings(req: SettingsRequest):
        known = {cls.name for cls in SOURCE_CLASSES}
        incoming = dict(req.settings or {})
        if isinstance(incoming.get("sources"), dict):
            incoming["sources"] = {k: bool(v) for k, v in incoming["sources"].items() if k in known}

        def mutate(cfg: dict) -> None:
            current = cfg.get("scraper") if isinstance(cfg.get("scraper"), dict) else {}
            merged = dict(current)
            merged.update(incoming)
            cfg["scraper"] = normalize_settings(merged)

        modify_config(mutate)
        svc = get_service()
        svc.invalidate_config()
        return {"status": "ok", "settings": svc.settings()}

    @router.post("/cache/clear")
    def clear_cache():
        return {"status": "ok", "removed": get_service().fetcher.clear_cache()}

    @router.post("/health/reset")
    def reset_health(source: str = ""):
        get_service().fetcher.reset_health(source or None)
        return {"status": "ok"}

    return router
