"""文件名 → 番号 / 媒体类型识别"""

from __future__ import annotations

import re
from typing import Optional

from .models import (
    KIND_FC2, KIND_HENTAI, KIND_JAV, KIND_MGS, KIND_UNCENSORED, KIND_UNKNOWN, KIND_WESTERN,
    MediaInfo,
)

# 常见站点 / 论坛水印前缀：hhd800.com@ABC-123、[xxx.com]ABC-123、kcf9.com ABC-123
_DOMAIN_PREFIX = re.compile(
    r'^(?:\[[^\]]*\]|【[^】]*】|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|cc|tv|xyz|me|top|club|vip|la|ws|fun|info|co|io|app|in|tw|hk|jp)[\]@_ \-]*)+',
    re.IGNORECASE,
)
# 编码 / 分辨率 / 压制组等噪声
_NOISE = re.compile(
    r'(?i)(?<![a-z0-9])(?:2160p|1080p|720p|540p|480p|4k|8k|uhd|fhd|hd|sd|h\.?264|h\.?265|x264|x265|hevc|avc|aac|dts|hdr|'
    r'web-?dl|webrip|bluray|blu-ray|xxx|mp4-ktr|ktr|prt|rarbg|sample)(?![a-z0-9])'
)
_PART = re.compile(r'(?i)(?:[-_ .](?:cd|part|pt|disc|disk)[-_ .]?(\d{1,2}))$')
_COPY_MARK = re.compile(r'(?:\s*[\(\[]\d{1,2}[\)\]])+$')
_EXT_SUFFIX = re.compile(r'\((?:mp4|mkv|avi|wmv|mov|flv|webm|m4v|ts|m2ts|mts|iso|rmvb|rm|asf|mpg|mpeg|f4v)\)$', re.IGNORECASE)
_VIDEO_EXT = re.compile(r'\.(?:mp4|mkv|avi|wmv|mov|flv|webm|m4v|ts|m2ts|mts|iso|rmvb|rm|asf|mpg|mpeg|f4v|strm)$', re.IGNORECASE)

# 不可能是番号前缀的词（编码、容器、常见英文）
_NOT_PREFIX = {
    "MP", "MP4", "MKV", "AVI", "WMV", "AAC", "DTS", "HDR", "WEB", "FHD", "UHD", "HEVC", "AVC", "CD", "PART",
    "DISC", "DISK", "VOL", "EP", "SEASON", "S", "E", "PT", "TS", "BD", "DVD", "FPS", "BIT", "KBPS", "HD", "SD",
    "XXX", "THE", "AND", "VR", "FC", "PPV", "JAV", "AV",
}

# MGStage 素人系（无数字前缀写法）
_MGS_PREFIXES = {
    "SIRO", "LUXU", "GANA", "MIUM", "MAAN", "ARA", "NTK", "DCV", "SCUTE", "KIRAY", "INSTV", "SWEET", "HHH",
    "NAMA", "ORE", "PAIS", "ENDX", "SIMM", "JAC", "MFC", "GOKU", "CUTE", "ORECO", "LOG", "DOC", "SQB",
}

_HENTAI_HINT = re.compile(r'(?i)里番|裏番|hentai|getchu|动画|動畫|アニメ|anime|二次元')

_CARIB_HINT = re.compile(r'(?i)carib|加勒比|カリビアン')
_CARIBPR_HINT = re.compile(r'(?i)caribpr|加勒比.?pr')
_1PON_HINT = re.compile(r'(?i)1pon|一本道')
_10MU_HINT = re.compile(r'(?i)10mu|天然むすめ|天然素人')
_PACO_HINT = re.compile(r'(?i)paco|パコパコ')
_MURA_HINT = re.compile(r'(?i)muramura|ムラムラ')
_TOKYO_HINT = re.compile(r'(?i)tokyo[-_ ]?hot|東京熱|东京热')


def strip_filename(filename: str) -> str:
    """去掉扩展名、TreeSTRM 的 (mp4) 后缀、网盘副本标记"""
    fn = _VIDEO_EXT.sub('', filename.strip())
    fn = _EXT_SUFFIX.sub('', fn).strip()
    fn = _COPY_MARK.sub('', fn).strip()
    return fn


def number_key(number: str) -> str:
    """番号比较键：忽略大小写、分隔符、数字前导零，FC2 统一去掉 PPV。
    无码日期番号保留分隔符：010120-001 是加勒比，010120_001 是一本道，不是同一部"""
    m = re.fullmatch(r'\s*(\d{6})([-_])(\d{2,3})\s*', number or '')
    if m:
        return "".join(m.groups())
    s = re.sub(r'[^A-Za-z0-9]', '', number or '').upper()
    s = s.replace('FC2PPV', 'FC2')
    m = re.match(r'^(.*?[A-Z])0*(\d+)$', s)
    if m:
        return m.group(1) + m.group(2)
    return s


def mgs_alias(number: str) -> str:
    """MGS 番号的数字前缀在聚合站常被省略：259LUXU-1500 → LUXU-1500；无前缀返回空"""
    m = re.fullmatch(r'\d{2,4}([A-Za-z]{2,6}-\d{3,5})', number or '')
    return m.group(1).upper() if m else ""


def numbers_match(a: str, b: str) -> bool:
    ka, kb = number_key(a), number_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    strip = lambda k: re.sub(r'^\d{2,4}(?=[A-Z])', '', k)  # noqa: E731
    return strip(ka) == strip(kb) and (strip(ka) != ka or strip(kb) != kb)


def _flags(fn: str) -> tuple:
    subtitle = bool(re.search(r'(?i)(?:[-_](?:c|uc|ch)$|中字|中文字幕|chs|cht)', fn))
    leak = bool(re.search(r'(?i)(?:[-_](?:u|uc)$|uncensored|無碼破解|无码破解|流出|leak)', fn))
    return subtitle, leak


def _strip_flags(fn: str) -> str:
    return re.sub(r'(?i)[-_](?:c|uc|u|ch)$', '', fn).strip()


def _detect_uncensored(fn: str, context: str) -> Optional[MediaInfo]:
    m = re.search(r'(?i)heyzo[-_ ]?(\d{4})', fn)
    if m:
        return MediaInfo(KIND_UNCENSORED, number=f"HEYZO-{m.group(1)}", studio="HEYZO")

    m = re.search(r'(?i)(?:^|[^a-z])(?:n|k)(\d{4})(?!\d)', fn)
    if m and _TOKYO_HINT.search(context):
        letter = re.search(r'(?i)([nk])\d{4}', fn).group(1).lower()
        return MediaInfo(KIND_UNCENSORED, number=f"{letter}{m.group(1)}", studio="Tokyo-Hot")

    m = re.search(r'(?<!\d)(\d{6})[-_](\d{2,3})(?!\d)', fn)
    if not m:
        return None
    date_part, seq = m.groups()
    sep = fn[m.start(2) - 1]
    if _CARIBPR_HINT.search(context):
        studio, number = "Caribbeancompr", f"{date_part}_{seq}"
    elif _CARIB_HINT.search(context):
        studio, number = "Caribbeancom", f"{date_part}-{seq}"
    elif _1PON_HINT.search(context):
        studio, number = "1Pondo", f"{date_part}_{seq}"
    elif _10MU_HINT.search(context):
        studio, number = "10musume", f"{date_part}_{seq.zfill(2)}"
    elif _PACO_HINT.search(context):
        studio, number = "Pacopacomama", f"{date_part}_{seq}"
    elif _MURA_HINT.search(context):
        studio, number = "Muramura", f"{date_part}_{seq}"
    elif sep == '-':
        studio, number = "Caribbeancom", f"{date_part}-{seq}"
    elif len(seq) == 2:
        studio, number = "10musume", f"{date_part}_{seq}"
    else:
        studio, number = "1Pondo", f"{date_part}_{seq}"
    # 加勒比/一本道番号为 MMDDYY-NNN，可直接推出发行日期
    mm, dd, yy = date_part[:2], date_part[2:4], date_part[4:]
    date = f"20{yy}-{mm}-{dd}" if 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31 else ""
    return MediaInfo(KIND_UNCENSORED, number=number, studio=studio, date=date)


def _detect_mgs(fn: str) -> Optional[MediaInfo]:
    m = re.search(r'(?i)(?<![a-z0-9])(\d{3})([a-z]{2,6})[-_ ]?(\d{3,5})(?!\d)', fn)
    if m:
        number = f"{m.group(1)}{m.group(2).upper()}-{m.group(3)}"
        return MediaInfo(KIND_MGS, number=number)
    m = re.search(r'(?i)(?<![a-z0-9])([a-z]{2,6})[-_ ]?(\d{3,5})(?!\d)', fn)
    if m and m.group(1).upper() in _MGS_PREFIXES:
        return MediaInfo(KIND_MGS, number=f"{m.group(1).upper()}-{m.group(2)}")
    return None


def _detect_jav(fn: str) -> Optional[MediaInfo]:
    # DMM 内容 ID：ssis00001 / 1start00001 → SSIS-001
    m = re.search(r'(?i)(?<![a-z0-9])\d{0,3}([a-z]{2,6})00(\d{3})(?!\d)', fn)
    if m and m.group(1).upper() not in _NOT_PREFIX:
        return MediaInfo(KIND_JAV, number=f"{m.group(1).upper()}-{m.group(2)}")

    candidates = []
    patterns = (
        # 纯字母前缀：ABP-123 / abp123 / ABP 123
        (r'(?i)(?<![a-z0-9])([a-z]{2,6})([-_ ]?)(\d{2,5})(?!\d)', False),
        # 字母数字混合前缀必须带分隔符：T28-123 / S2M-012
        (r'(?i)(?<![a-z0-9])([a-z]\d{1,2}[a-z]?)([-_])(\d{2,5})(?!\d)', True),
    )
    for pattern, _mixed in patterns:
        for m2 in re.finditer(pattern, fn):
            prefix, sep, num = m2.group(1).upper(), m2.group(2), m2.group(3)
            if prefix in _NOT_PREFIX:
                continue
            # 无分隔符 / 空格分隔时要求 3 位以上数字且不能是年份，避免普通单词+数字误判
            if sep not in ('-', '_') and (len(num) < 3 or re.fullmatch(r'(?:19|20)\d{2}', num)):
                continue
            candidates.append((0 if sep in ('-', '_') else 1, m2.start(), prefix, num))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    _, _, prefix, num = candidates[0]
    num = num.lstrip('0').zfill(3) if len(num) > 3 else num.zfill(3)
    return MediaInfo(KIND_JAV, number=f"{prefix}-{num}")


def _detect_western(fn: str, parent_dir: str) -> MediaInfo:
    m = re.search(r'(?:^|[._ \-])((?:19|20)?\d{2})[._ \-](\d{2})[._ \-](\d{2})(?:[._ \-]|$)', fn)
    date = ""
    if m:
        y, mo, d = m.groups()
        if len(y) == 2:
            y = "20" + y
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            date = f"{y}-{mo}-{d}"
    if date:
        site = fn[:m.start()].strip('._- ')
        rest = fn[m.end():].strip('._- ')
    else:
        site, rest = "", fn
        # 「Site - Performer - Title」写法
        parts = re.split(r'\s+-\s+', fn)
        if len(parts) >= 2 and re.fullmatch(r'[A-Za-z0-9 .&\']{2,40}', parts[0]):
            site, rest = parts[0], ' - '.join(parts[1:])
    if not site and parent_dir and re.fullmatch(r'[A-Za-z0-9 ._&\'-]{3,40}', parent_dir):
        site = parent_dir
    title = _NOISE.sub(' ', rest)
    title = re.sub(r'[._]+', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -')
    site = re.sub(r'[._]+', ' ', site).strip()
    return MediaInfo(KIND_WESTERN, site=site, studio=site, date=date, title_hint=title)


def identify(filename: str, parent_dir: str = "", path_hint: str = "") -> MediaInfo:
    """识别文件名。parent_dir 为上级目录名，path_hint 为专区内相对路径（用于识别厂牌提示词）"""
    base = strip_filename(filename)
    fn = _DOMAIN_PREFIX.sub('', base).strip() or base
    part = ""
    pm = _PART.search(fn)
    if pm:
        part = pm.group(1)
        fn = fn[:pm.start()].strip()
    subtitle, leak = _flags(fn)
    fn_core = _strip_flags(fn)
    context = " ".join([fn_core, parent_dir or "", path_hint or ""])

    def done(info: MediaInfo) -> MediaInfo:
        info.raw = fn_core
        info.subtitle = subtitle
        info.uncensored_leak = leak
        info.part = part
        return info

    m = re.search(r'(?i)fc2[-_ ]?(?:ppv)?[-_ ]?(\d{5,8})(?!\d)', fn_core)
    if not m and re.search(r'(?i)fc2', parent_dir or ""):
        m = re.search(r'(?<!\d)(\d{6,8})(?!\d)', fn_core)
    if m:
        return done(MediaInfo(KIND_FC2, number=f"FC2-PPV-{m.group(1)}"))

    cleaned = _NOISE.sub(' ', fn_core)
    unc = _detect_uncensored(cleaned, context)
    if unc:
        return done(unc)

    if _HENTAI_HINT.search(" ".join([parent_dir or "", path_hint or ""])):
        title = re.sub(r'[._]+', ' ', cleaned).strip()
        return done(MediaInfo(KIND_HENTAI, number=title, title_hint=title))

    mgs = _detect_mgs(cleaned)
    if mgs:
        return done(mgs)

    # 欧美日期格式优先于番号：Vixen.22.01.01 不能被识别成番号
    if re.search(r'[A-Za-z]{3,}[._ ](?:19|20)?\d{2}[._ ]\d{2}[._ ]\d{2}(?:[._ ]|$)', cleaned):
        return done(_detect_western(cleaned, parent_dir))

    jav = _detect_jav(cleaned)
    if jav:
        return done(jav)

    western = _detect_western(cleaned, parent_dir)
    # 欧美条目必须有站点或日期，或至少是英文标题；纯中日文标题无从检索
    if western.site or western.date or re.search(r'[A-Za-z]{3,}', western.title_hint):
        return done(western)
    return done(MediaInfo(KIND_UNKNOWN))


def info_from_override(number: str, current: MediaInfo) -> MediaInfo:
    """手动指定番号 / 关键词后重新识别，保留原文件的字幕等标记"""
    info = identify(number, "", "")
    info.subtitle = info.subtitle or current.subtitle
    info.uncensored_leak = info.uncensored_leak or current.uncensored_leak
    info.part = current.part
    if info.kind in (KIND_UNKNOWN, KIND_WESTERN) and current.kind == KIND_WESTERN:
        info.kind = KIND_WESTERN
    return info
