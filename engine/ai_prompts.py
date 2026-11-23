#!/usr/bin/env python3
"""
AI 元数据治理的提示词与后处理（纯函数，不依赖 app.py，便于离线试跑与单测）。

设计要点：
- 分类不让 AI 自由发挥：只能从 GENRE_VOCAB 里选，库有默认分类时始终排第一，同库分类一致
- 目录级结果（制作方/出镜者/通用标签）传给文件级，避免把网名按字面意思理解成题材
- 所有条目都交给 AI；纯编号文件名由提示词约束：保留原名，可借目录信息概括，但不编造画面细节
- 标签有黑名单：泛标签、库名、人名一律过滤
"""

import os
import re
from typing import Dict, Iterable, List, Optional, Sequence

from engine.meta_rules import clean_people, clean_studio

# 写进 NFO 的隐藏版本标记：带当前版本标记的 NFO 已按新提示词生成，批量重写时跳过（断点续跑）
META_MARK_TAG = "treestrm_meta"
META_VERSION = "5"

# 可选分类词表：AI 只能从这里选
GENRE_VOCAB = [
    "舞蹈", "直播", "写真", "私拍", "自拍", "Cosplay", "街拍", "偷拍", "厕拍", "AI真人", "AI动漫", "AI换脸",
    "3D动漫", "PMV", "ASMR", "足控", "丝袜", "车模", "女团直拍", "天体", "按摩", "剧情", "欧美", "无码",
    "调教", "模特", "生活纪实", "恶作剧",
]

# 库（成人/<库名>）的默认分类；None 表示混合库，全靠 AI 从词表里选
CATEGORY_GENRE: Dict[str, Optional[str]] = {
    "3d动漫": "3D动漫", "AI": None, "AI真人": "AI真人", "ASMR": "ASMR", "PMV": "PMV", "cd": None, "cp": "偷拍",
    "个人向": None, "中字欧美": "欧美", "写真": "写真", "女团": "女团直拍", "抖音风裸舞弹力插": "舞蹈",
    "无码合集": "无码", "杂乱小视频": None, "欧美": "欧美", "欧美未刮削": "欧美", "水浴沙滩": "天体",
    "油管收藏": None, "玩物直播": "直播", "直播": "直播", "直播平台": "直播", "直播热舞BJ": "直播",
    "私藏": None, "系列": None, "舞蹈": "舞蹈", "街拍": "街拍", "足控": "足控", "车模": "车模",
}

# 泛标签黑名单（小写比较）
GENERIC_TAGS = {
    "成人", "成人向", "成人内容", "成人视频", "成人影片", "成人短视频", "nsfw", "色情", "福利", "合集", "系列",
    "系列合集", "片段", "精选", "高清", "超清", "4k", "4k原画", "1080p", "720p", "视频", "短视频", "原版", "无水印",
    "最新", "流出", "最新流出", "资源", "推荐", "未知", "其他", "剧情",
}

DIR_SYSTEM = "你是严谨的媒体库元数据编辑，只输出一个合法 JSON 对象，不输出任何解释或 markdown。JSON 压缩成一行：不要缩进、不要换行、不要用代码块包裹。"
ITEM_SYSTEM = "你是严谨的媒体库元数据编辑，严格按编号输出一个合法 JSON 对象，不输出任何解释或 markdown。JSON 压缩成一行：不要缩进、不要换行、不要用代码块包裹。"

ITEM_RULES = """对每个文件输出：
1. clean_title：只清洗、不改写。
   - 删除：扩展名、分辨率/编码（4K、1080P、HEVC）、网址和水印（xxx.com、@xxx）、「-audio」等技术后缀、哈希串、视频 ID（如 [ua-v2RMSwpg]）、剪辑时间戳（如 -00.00.10.592-00.13.16.104）、广告词（最新流出、推荐、顶流、重磅、资源、✅ 等）。
   - 必须保留：人名/网名、品牌、角色名、期号、集数、编号、日期、相机编号（IMG_9558、DSC_0012）、末尾的 (n) 序号——这些用来区分同系列的不同视频。
   - 日文/韩文/英文翻译成简洁中文；人名、品牌、编号保持原文。
   - 文件名已经是通顺中文时基本原样保留。
   - 同一批里不同文件的标题不能完全相同。
2. tags：这个视频的具体特征（服饰、道具、场景、动作、拍摄方式），0~4 个，优先从文件名提取；文件名看不出时可以结合目录信息合理判断，没把握就给 []（目录通用标签会自动补上）。禁止泛标签（成人、成人向、合集、片段、精选、高清、视频）和出镜者/博主名；但 Cosplay 角色名、作品名（如 霞、妲己、2B、原神）可以作为标签。
3. actors：这个文件名里明确出现、但不在「已知出镜者」里的演员/模特/博主名（如欧美演员 Mia Bandini），原样保留；没有就 []。角色名（Jinx、2B、妲己、云曦、纳兰嫣然）不是演员，放 tags；AI 生成、动漫、3D 内容的 actors 一律 []。
4. summary：一句 20 字以内的客观简介。文件名有具体内容就据此写；文件名只是编号/日期时，可以结合目录信息写一句概括（如「刑具不醒的竖屏裸舞短视频」），但不要编造文件名里没有的画面细节；实在没有信息就给 ""。不要写库名，不要写「编号001」「第1段」这种只复述编号的话。

示例：
- 「刑具不醒-3 (4)」（目录：刑具不醒的抖音风裸舞）→ {"clean_title":"刑具不醒-3 (4)","tags":[],"actors":[],"summary":"刑具不醒的抖音风裸舞短视频。"}
- 「钛合金TiTi NO.101 妲己 IMG_9558」→ {"clean_title":"钛合金TiTi NO.101 妲己 IMG_9558","tags":["妲己","古风","Cosplay"],"actors":[],"summary":"妲己角色的古风Cosplay写真。"}
- 「钛合金TiTi NO.060 沙滩排球~绿色肉色 040」→ {"clean_title":"钛合金TiTi NO.060 沙滩排球 绿色肉色 040","tags":["沙滩排球服","肉色丝袜"],"summary":"沙滩排球主题写真，绿色与肉色两套造型。"}
- 「(4K) 캠핑에서는 모카포트가 최고지 [ua-v2RMSwpg]」→ {"clean_title":"露营还是摩卡壶最好","tags":["露营","户外"],"summary":"露营时用摩卡壶煮咖啡的日常。"}
- 「【最新流出 明星气质】顶级PANS极品御姐『暖暖』最新大尺度 黑色蕾丝内裤 透明学生装 高清1080P原版」→ {"clean_title":"PANS 暖暖 黑色蕾丝内裤 透明学生装","tags":["黑色蕾丝","透明学生装"],"summary":"暖暖身穿透明学生装与黑色蕾丝内衣的私拍。"}
- 「热舞猫 蕾蕾 第2期 (7)」→ {"clean_title":"热舞猫 蕾蕾 第2期 (7)","tags":[],"actors":["蕾蕾"],"summary":"热舞猫蕾蕾第2期舞蹈片段。"}
- 「purple_bitch_Jinx-and-Mia-Bandini-threesome-1080p」（已知出镜者 Purple Bitch）→ {"clean_title":"Purple Bitch - Jinx and Mia Bandini threesome","tags":["Jinx","三人行"],"actors":["Mia Bandini"],"summary":"Jinx角色与Mia Bandini的三人场景。"}
- 「9月 (18)」（目录信息不足）→ {"clean_title":"9月 (18)","tags":[],"summary":""}"""


def category_of(folder_rel_path: str) -> str:
    """'成人/舞蹈/精舞社/x' -> '舞蹈'。"""
    parts = [p for p in folder_rel_path.replace("\\", "/").split("/") if p]
    return parts[1] if len(parts) > 1 and parts[0] == "成人" else (parts[0] if parts else "")


def build_dir_prompt(folder_rel_path: str, file_names: Sequence[str]) -> str:
    category = category_of(folder_rel_path)
    default = CATEGORY_GENRE.get(category)
    sample = "\n".join(f"- {os.path.basename(f)}" for f in list(file_names)[:60])
    genre_hint = f"本库默认分类是「{default}」，可再补 0~1 个" if default else "从词表里选 1~2 个最贴切的"
    return f"""任务：为媒体库里的一个目录生成目录级元数据。同一目录的视频通常来自同一品牌/博主/系列。

目录路径：{folder_rel_path}
（第一级「成人」之后是库名「{category}」，库名不是标签。）
文件名样本：
{sample}

字段规则：
1. studio：制作方/品牌/工作室/舞团/主播名。优先取目录名里的品牌或博主名（如 PANS、热舞猫、钛合金TiTi、刑具不醒）。目录名只是日期、编号或「合集/大合集/流出」之类泛称时输出 ""。禁止输出「未知」等占位词。
2. actors：整个目录共同的出镜者/博主/模特名数组（通常就是目录名对应的人）。只填明确出现的人名或网名，不要猜；只在个别文件名里出现的客串演员不要填（文件级会单独识别）；没有就 []。网名原样保留——「刑具不醒」「一口白桃七」「晚晚睡不醒」都是网名，不是普通词语，不能按字面意思理解成题材。主播/博主目录的目录名即使看起来像普通词（如「舞蹈老师」「高数3712」）也是网名，要填进 actors。AI 生成内容（AI去衣、AI换脸等）的制作者只填 studio，不算出镜者。动漫/游戏/小说角色名（如 司芸香、云曦、纳兰嫣然、小白、2B）不是出镜者，AI动漫、3D动漫、Cosplay 目录里的角色名放 common_tags 或留给文件级 tags，actors 给 []。
3. genres：{genre_hint}，只能从这个词表里选：{"、".join(GENRE_VOCAB)}。
4. common_tags：本目录几乎每个视频都适用的具体内容标签 0~4 个（如 竖屏、热舞、绳艺、黑丝、直播回放、AI生成、翻跳）。只有一部分视频适用的不要填；没把握就 []。禁止：成人、成人向、合集、系列合集、片段、精选、高清、4K、视频、剧情、库名、人名、已经选进 genres 的词。
5. summary：一句话说明这些视频是什么（谁的什么内容），20 字以内，不要堆砌形容词，如「Purple Bitch 的动漫角色 Cosplay 自拍」。不要出现「目录」「成人」「合集」「视频集」这类词；说不清就 ""。

只输出 JSON：{{"studio":"","actors":[],"genres":[],"common_tags":[],"summary":""}}"""


def build_item_prompt(folder_rel_path: str, file_names: Sequence[str], ctx: Optional[dict] = None) -> str:
    ctx = ctx or {}
    studio = ctx.get("studio") or "无"
    actors = "、".join(ctx.get("actors") or []) or "无"
    common = "、".join(ctx.get("common_tags") or []) or "无"
    rows = "\n".join(f"{i + 1}. {os.path.basename(f)}" for i, f in enumerate(file_names))
    return f"""任务：为同一目录下的视频文件生成展示标题、专属标签和简介。

目录：{folder_rel_path}
已知制作方：{studio}
已知出镜者/博主：{actors}（这些是名字，不要按字面意思理解成题材，也不要写进 tags）
目录通用标签（已经有了，不要重复输出）：{common}

{ITEM_RULES}

输出格式（键是下面列表的编号）：{{"1":{{"clean_title":"","tags":[],"actors":[],"summary":""}},"2":{{...}}}}

待处理文件：
{rows}"""


def build_fallback_prompt(title_rows: Sequence[str]) -> str:
    """翻译兜底第二步：基于已翻译的中文标题整理元数据。"""
    return ("这是媒体库元数据整理任务。下面每行给出原路径和已翻译的中文标题，请按规则整理。\n\n"
            + ITEM_RULES
            + "\n\n输出格式（键是编号）：{\"1\":{\"clean_title\":\"\",\"tags\":[],\"actors\":[],\"summary\":\"\"}}\n\n"
            + "\n".join(title_rows))


# ---------------- 后处理 ----------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def clean_tags(tags: Iterable[object], *, category: str = "", names: Iterable[str] = (), limit: int = 6) -> List[str]:
    """去泛标签、去库名和人名、去重，最多 limit 个。"""
    banned = {_norm(x) for x in GENERIC_TAGS}
    banned.add(_norm(category))
    banned.update(_norm(n) for n in names if n)
    out: List[str] = []
    seen = set()
    for t in tags or []:
        s = str(t or "").strip().strip("#")
        k = _norm(s)
        if not s or k in banned or k in seen or len(s) > 20:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= limit:
            break
    return out


# 「无码」是 AV 打码术语，只在可能收 AV 的库里由 AI 选；抄底等库里它是误判（11-23 cd 1158 条）
UNCENSORED_OK = {"无码合集", "系列", "私藏"}
# 这些库的「作者」是搬运/生成者，不是出镜的人：与制作方同名的演员要去掉
AI_CATEGORIES = {"AI", "AI真人", "3d动漫"}


def pick_genres(category: str, ai_genres: object) -> List[str]:
    """库默认分类排第一，其余只收词表内的，最多 2 个；都没有时退回库名。"""
    vocab = {g.lower(): g for g in GENRE_VOCAB}
    if isinstance(ai_genres, str):
        ai_genres = re.split(r"[/、,，]", ai_genres)
    out: List[str] = []
    default = CATEGORY_GENRE.get(category)
    if default:
        out.append(default)
    for g in ai_genres if isinstance(ai_genres, list) else []:
        v = vocab.get(str(g or "").strip().lower())
        if v == "无码" and category not in UNCENSORED_OK:
            continue
        if v and v not in out:
            out.append(v)
    out = out[:2]
    return out or ([category] if category else [])


def dir_context(ai_res: Optional[dict]) -> dict:
    """目录级 AI 结果 → 规范化上下文；单作者目录用作者名兜底制作方。"""
    if not isinstance(ai_res, dict):
        return {}
    actors = clean_people(ai_res.get("actors"))
    studio = clean_studio(ai_res.get("studio", "")) or (actors[0] if len(actors) == 1 else "")
    common = ai_res.get("common_tags") if isinstance(ai_res.get("common_tags"), list) else []
    return {
        "studio": studio,
        "actors": actors,
        "common_tags": common,
        "genres": ai_res.get("genres") or ai_res.get("genre"),
        "summary": str(ai_res.get("summary") or ai_res.get("clean_summary") or "").strip(),
    }


def assemble_meta(item_ai: dict, ctx: dict, category: str, title: str, failed: bool = False) -> dict:
    """文件级 AI 结果 + 目录上下文 → 写 NFO 用的 meta（标题需调用方先去重）。"""
    ctx = ctx or {}
    ai_tags = item_ai.get("tags") if isinstance(item_ai.get("tags"), list) else []
    item_actors = item_ai.get("actors") if isinstance(item_ai.get("actors"), list) else []
    actors = clean_people(list(ctx.get("actors") or []) + item_actors)
    if category in AI_CATEGORIES:
        actors = [a for a in actors if a != ctx.get("studio", "")]
    genres = pick_genres(category, ctx.get("genres"))
    names = [ctx.get("studio", ""), *actors, *genres]
    tags = clean_tags(ai_tags + list(ctx.get("common_tags") or []), category=category, names=names)
    summary = str(item_ai.get("summary") or "").strip() or ctx.get("summary", "")
    if failed:
        tags = list(dict.fromkeys(tags + ["AI待补"]))
        summary = (summary or "") + "（AI提炼失败，待补全）"
    return {
        "title": title,
        "studio": ctx.get("studio", ""),
        "actors": actors,
        "genres": genres,
        "tags": tags,
        "summary": summary,
    }
