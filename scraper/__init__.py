"""TreeSTRM 官方刮削模块

identify   文件名 → 番号 / 站点 / 日期
sources/*  各站点解析器（官方站优先，聚合站补全）
merge      候选打分（置信度）+ 多站点字段合并
output     海报裁剪、fanart、完整 NFO
store      SQLite 状态库（结论、候选、人工修正、任务）
runner     任务队列与人工修正入口
api        /api/scraper/* 接口
"""

from .identify import identify, number_key
from .runner import ScraperService

__all__ = ["ScraperService", "identify", "number_key"]
