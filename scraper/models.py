"""刮削模块的数据结构"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# 媒体类型
KIND_FC2 = "fc2"
KIND_JAV = "jav"              # 日本有码标准番号
KIND_MGS = "mgs"              # MGStage 素人系（SIRO / 259LUXU / 300MIUM …）
KIND_UNCENSORED = "uncensored"  # 无码厂牌（加勒比 / 一本道 / HEYZO …）
KIND_WESTERN = "western"      # 欧美 Scene（Studio.YY.MM.DD.Performer）
KIND_HENTAI = "hentai"        # 二次元里番
KIND_UNKNOWN = "unknown"

KIND_LABELS = {
    KIND_FC2: "FC2",
    KIND_JAV: "日本有码",
    KIND_MGS: "MGS 素人",
    KIND_UNCENSORED: "日本无码",
    KIND_WESTERN: "欧美 Scene",
    KIND_HENTAI: "里番",
    KIND_UNKNOWN: "未识别",
}


@dataclass
class MediaInfo:
    """从文件名（及上级目录名）识别出的信息"""
    kind: str
    number: str = ""          # 规范化番号，如 SSIS-001 / FC2-PPV-1234567 / 010120-001
    studio: str = ""          # 无码厂牌或欧美站点
    site: str = ""            # 欧美站点原始写法
    date: str = ""            # YYYY-MM-DD
    title_hint: str = ""      # 欧美：去掉站点和日期后的剩余部分（通常是演员 + 标题）
    raw: str = ""             # 清洗后的文件名
    subtitle: bool = False    # 文件名带中文字幕标记（-C / -UC / 中字）
    uncensored_leak: bool = False  # 有码流出 / 破解（-U / -UC）
    part: str = ""            # CD1 / part2 等分段标记

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Actor:
    name: str
    thumb: str = ""
    role: str = ""


@dataclass
class Metadata:
    """单个站点返回的元数据。字段缺失时留空，由合并器从其他站点补齐"""
    source: str
    number: str = ""
    title: str = ""
    original_title: str = ""
    plot: str = ""
    studio: str = ""          # 制作商
    label: str = ""           # 发行商 / 厂牌
    series: str = ""
    director: str = ""
    premiered: str = ""       # YYYY-MM-DD
    runtime: int = 0          # 分钟
    rating: float = 0.0       # 0-10
    genres: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    actors: List[Actor] = field(default_factory=list)
    poster_url: str = ""      # 竖版海报
    cover_url: str = ""       # 日式横版封套（右半边即正面），可裁出竖版海报，同时作为 fanart
    fanart_url: str = ""      # 普通横图（剧照 / 背景），只作 fanart，不裁剪
    extrafanart: List[str] = field(default_factory=list)
    url: str = ""             # 详情页
    source_id: str = ""       # 站点内部 ID（TPDB uuid / JavDB 路径等），用于手动指定
    image_referer: str = ""   # 部分图床校验 Referer
    score: float = 0.0        # 与识别结果的匹配置信度 0-1，由合并器填写

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def has_image(self) -> bool:
        return bool(self.poster_url or self.cover_url or self.fanart_url)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Metadata":
        data = dict(data)
        data["actors"] = [a if isinstance(a, Actor) else Actor(**a) for a in data.get("actors") or []]
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ScrapeOutcome:
    """一次刮削的最终结论"""
    status: str                      # matched / low_confidence / not_found / error
    info: MediaInfo
    meta: Optional[Metadata] = None  # 合并后的结果
    candidates: List[Metadata] = field(default_factory=list)  # 参与合并或被拒绝的候选（按得分降序）
    images: List[Dict[str, str]] = field(default_factory=list)  # 可用图片 {url, type: poster/cover/fanart, referer, source}，按优先级
    sources_used: List[str] = field(default_factory=list)
    message: str = ""
