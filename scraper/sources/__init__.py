"""站点注册表与各类型的查询优先级"""

from __future__ import annotations

from typing import Dict, List, Type

from ..models import KIND_FC2, KIND_HENTAI, KIND_JAV, KIND_MGS, KIND_UNCENSORED, KIND_WESTERN, MediaInfo
from .base import Context, Source
from .fc2 import FC2Club, FC2Mirror, FC2Official, FC2PPVDB, FC2Snapshot
from .getchu import Getchu
from .javbus import Avsox, JavBus
from .javdb import JavDB
from .makers import Faleno, MGStage, Prestige
from .tpdb import ThePornDB
from .uncensored import Caribbean, D2Pass, Heyzo

SOURCE_CLASSES: List[Type[Source]] = [
    JavBus, JavDB, Avsox,
    FC2Official, FC2PPVDB, FC2Club, FC2Mirror, FC2Snapshot,
    Caribbean, D2Pass, Heyzo,
    MGStage, Prestige, Faleno,
    Getchu, ThePornDB,
]

# 每种类型按顺序查询；官方/厂商站排前面（字段最准），聚合站补全
PRIORITY: Dict[str, List[str]] = {
    KIND_FC2: ["fc2", "fc2ppvdb", "javdb", "fc2club", "fc2mirror", "fc2snapshot"],
    KIND_JAV: ["prestige", "faleno", "mgstage", "javbus", "javdb", "avsox"],
    KIND_MGS: ["mgstage", "javdb", "javbus"],
    KIND_UNCENSORED: ["caribbean", "1pondo", "heyzo", "javbus", "javdb", "avsox"],
    KIND_HENTAI: ["getchu", "javdb"],
    KIND_WESTERN: ["tpdb"],
}


class Registry:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.sources: Dict[str, Source] = {cls.name: cls(ctx) for cls in SOURCE_CLASSES}

    def enabled(self, name: str) -> bool:
        src = self.sources.get(name)
        if not src:
            return False
        flags = (self.ctx.settings().get("scraper") or {}).get("sources") or {}
        value = flags.get(name)
        return src.default_enabled if value is None else bool(value)

    def chain(self, info: MediaInfo) -> List[Source]:
        """该条目可用的站点（已启用、支持该类型），按优先级排列"""
        out = []
        for name in PRIORITY.get(info.kind, []):
            src = self.sources.get(name)
            if src and self.enabled(name) and src.supports(info):
                out.append(src)
        return out

    def describe(self) -> List[Dict]:
        health = self.ctx.fetcher.health_snapshot()
        kinds_of: Dict[str, List[str]] = {}
        for kind, names in PRIORITY.items():
            for n in names:
                kinds_of.setdefault(n, []).append(kind)
        return [
            {
                "name": s.name,
                "label": s.label,
                "kinds": kinds_of.get(s.name, []),
                "enabled": self.enabled(s.name),
                "default_enabled": s.default_enabled,
                "note": s.note,
                "health": health.get(s.name) or {},
            }
            for s in self.sources.values()
        ]
