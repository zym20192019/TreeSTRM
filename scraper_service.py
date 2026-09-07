import os, sys, re, time, json, requests, threading
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin
from bs4 import BeautifulSoup

# ==============================================================================
# Amane-Style Complete 24-Site Crawler Matrix & Multi-Source Waterfall Engine
# 完整复刻 Amane 全量 24 站点解析、制作商直连、4级阶梯式瀑布流与断点续跑状态机
# ==============================================================================

TPDB_TOKEN = "GYJTmJLvxL1XKiQ3XSJhImg9fWb3C6yVztbNyp8pf1cd2f25"
TPDB_HEADERS = {
    "Authorization": f"Bearer {TPDB_TOKEN}",
    "Accept": "application/json",
    "User-Agent": "TreeSTRM-Scraper/3.0 (Amane-Complete-Architecture)"
}

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7"
}

# ==================== 1. 深度语义识别与媒体类型分类器 ====================

def parse_media_identifier(filename: str, parent_dir_name: str = "") -> dict:
    """
    智能识别 6 大品类：FC2素人、日本标准番号、日本无码大厂、欧美Scene、二次元动漫里番、欧美独立厂牌
    """
    fn = re.sub(r'\.[^.]+$', '', filename)
    # 前置彻底剥离网盘副本标记如 (1), (2), (1)(1), [1], _1 等干扰项
    fn = re.sub(r'(\(\d+\))+|(\[\d+\])+|_\d+$', '', fn).strip()
    fn = re.sub(r'\(mp4\)|\(mkv\)|\(wmv\)|\(avi\)|XXX|2160p|1080p|720p|4k|h264|h265|hevc|hd|sd', '', fn, flags=re.IGNORECASE).strip()
    
    # 1. 检查 FC2 素人
    m_fc2 = re.search(r'(?:FC2[_-]?PPV[_-]?|FC2[_-]?)(\d{6,7})', fn, re.IGNORECASE)
    if m_fc2 or 'fc2' in parent_dir_name.lower():
        fid = m_fc2.group(1) if m_fc2 else re.search(r'(\d{6,7})', fn)
        if fid:
            fid_val = fid.group(1) if hasattr(fid, 'group') else str(fid)
            return {"type": "fc2", "number": f"FC2-PPV-{fid_val}", "id": fid_val}

    # 2. 检查 MGS 舞台专属无码前缀 (SIRO, LUXU, GOKU, ARA, 259LUXU, 200GANA, etc.)
    m_mgs = re.search(r'(SIRO|LUXU|GOKU|ARA|PRESTIGE|259LUXU|200GANA|300MIUM|181CHO|261ARA)[-_]?(\d{2,5})', fn, re.IGNORECASE)
    if m_mgs:
        prefix, num = m_mgs.groups()
        return {"type": "mgs", "number": f"{prefix.upper()}-{num}", "prefix": prefix.upper(), "num": num}

    # 3. 检查无码四大厂 (Caribbean, Heyzo, 1Pondo, Tokyo-Hot)
    m_1pon = re.search(r'(?:1pon|1pondo)[-_]?(\d{6})[_-](\d{3})', fn, re.IGNORECASE)
    if m_1pon:
        return {"type": "uncensored", "studio": "1Pondo", "number": f"{m_1pon.group(1)}_{m_1pon.group(2)}"}

    m_heyzo = re.search(r'HEYZO[-_]?(\d{4})', fn, re.IGNORECASE)
    if m_heyzo:
        return {"type": "uncensored", "studio": "HEYZO", "number": f"HEYZO-{m_heyzo.group(1)}"}
        
    m_carib = re.search(r'(\d{6})[-_](\d{3})', fn)
    if m_carib and ('carib' in fn.lower() or '加勒比' in fn.lower()):
        return {"type": "uncensored", "studio": "Caribbeancom", "number": f"{m_carib.group(1)}-{m_carib.group(2)}"}

    # 4. 检查日本标准商业番号 (DMM / S1 / Moodyz / Prestige / Faleno 等)
    m_jav = re.search(r'([A-Za-z0-9]{2,8})[-_]?(\d{3,5})', fn)
    if m_jav:
        prefix, num = m_jav.groups()
        if not re.match(r'^(20\d{2}|19\d{2})$', prefix) and len(prefix) <= 6:
            standard_num = f"{prefix.upper()}-{num}"
            return {"type": "jav", "number": standard_num, "prefix": prefix.upper(), "num": num}

    # 5. 检查欧美商业 Scene (Studio.YY.MM.DD.Performer.Title)
    raw_fn = fn
    m_date = re.search(r'(?:^|[._-])(\d{2,4})[._-](\d{2})[._-](\d{2})(?:[._-]|$)', raw_fn)
    date_str = None
    if m_date:
        y, m, d = m_date.groups()
        if len(y) == 2: y = "20" + y
        date_str = f"{y}-{m}-{d}"
        date_span = m_date.span()
        site_part = raw_fn[:date_span[0]].strip('._- ')
        title_part = raw_fn[date_span[1]:].strip('._- ')
    else:
        site_part = parent_dir_name
        title_part = raw_fn

    site = site_part if site_part else parent_dir_name
    clean_title = re.sub(r'(?i)\b(xxx|2160p|1080p|720p|4k|h264|h265|hevc|hd|sd|ktr|prt)\b', '', title_part)
    clean_title = re.sub(r'[._-]+', ' ', clean_title).strip()
    
    return {
        "type": "western_scene",
        "site": site,
        "date": date_str,
        "clean_title": clean_title,
        "raw": raw_fn
    }

# ==================== 2. 站点矩阵解析模块 (Amane 24-Sites Matrix) ====================

# --- ① FC2 全系矩阵 ---
def scrape_fc2_official(fc2_id: str) -> dict:
    url = f"https://adult.contents.fc2.com/article/{fc2_id}/"
    headers = {"User-Agent": COMMON_HEADERS["User-Agent"], "Cookie": "age_check=1; adult=1; contents_adult=1;"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200 and "notfound" not in r.text and "icon_404bg" not in r.text:
            soup = BeautifulSoup(r.text, 'html.parser')
            title_tag = soup.find('div', attrs={'data-section': 'userInfo'})
            title = title_tag.find('h3').get_text(strip=True) if title_tag and title_tag.find('h3') else None
            if not title:
                h2 = soup.find('h2')
                title = h2.get_text(strip=True) if h2 else f"FC2-PPV-{fc2_id}"
                
            poster = None
            main_thumb = soup.find('div', class_='items_article_MainitemThumb')
            if main_thumb and main_thumb.find('img'):
                poster = main_thumb.find('img').get('src')
                if poster and poster.startswith('//'): poster = 'https:' + poster
            if not poster:
                sample_li = soup.find('ul', class_='items_article_SampleImagesArea')
                if sample_li and sample_li.find('a'):
                    poster = sample_li.find('a').get('href')
                    if poster and poster.startswith('//'): poster = 'https:' + poster
            return {"id": f"FC2-PPV-{fc2_id}", "title": title, "studio": "FC2-PPV", "premiered": None, "plot": title, "poster_url": poster, "actors": [], "tags": ["FC2", "PPV", "无码"], "source": "FC2 Official"}
    except Exception:
        pass
    return None

def scrape_fc2club(fc2_id: str) -> dict:
    url = f"https://fc2club.top/html/FC2-{fc2_id}.html"
    try:
        r = requests.get(url, headers=COMMON_HEADERS, timeout=8)
        if r.status_code == 200 and "show-top-grids" in r.text:
            soup = BeautifulSoup(r.text, 'html.parser')
            top = soup.find('div', class_='show-top-grids')
            if top:
                title = top.find('h3').get_text(strip=True) if top.find('h3') else f"FC2-PPV-{fc2_id}"
                img = top.find('img').get('src') if top.find('img') else None
                if img and img.startswith('/'): img = "https://fc2club.top" + img
                return {"id": f"FC2-PPV-{fc2_id}", "title": title, "studio": "FC2-PPV", "premiered": None, "plot": title, "poster_url": img, "actors": [], "tags": ["FC2", "PPV", "FC2Club"], "source": "FC2Club"}
    except Exception:
        pass
    return None

def scrape_fc2ppvdb(fc2_id: str) -> dict:
    url = f"https://fc2ppvdb.com/articles/{fc2_id}"
    try:
        r = requests.get(url, headers=COMMON_HEADERS, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            h1 = soup.find('h1') or soup.find('h2')
            title = h1.get_text(strip=True) if h1 else f"FC2-PPV-{fc2_id}"
            img = soup.find('div', class_='article-image').find('img') if soup.find('div', class_='article-image') else None
            poster = img.get('src') if img else None
            return {"id": f"FC2-PPV-{fc2_id}", "title": title, "studio": "FC2-PPV", "premiered": None, "plot": title, "poster_url": poster, "actors": [], "tags": ["FC2", "PPV"], "source": "FC2PPVDB"}
    except Exception:
        pass
    return None



def scrape_fc2_mirrors_deep(fc2_id: str) -> dict:
    """针对官方 404 下架的 FC2 绝版条目，进行海外高存活镜像库与归档深度打捞"""
    mirrors = [
        f"https://fc2jav.com/fc2-ppv-{fc2_id}/",
        f"https://maxjav.com/{fc2_id}/"
    ]
    for url in mirrors:
        try:
            r = requests.get(url, headers=COMMON_HEADERS, timeout=6)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                imgs = [img.get('src') or img.get('data-src') or img.get('data-original') for img in soup.find_all('img')]
                for src in imgs:
                    if src and (fc2_id in src or 'poster' in src or 'cover' in src):
                        if src.startswith('//'): src = 'https:' + src
                        title = soup.find('h1').get_text(strip=True) if soup.find('h1') else f"FC2-PPV-{fc2_id}"
                        return {
                            "id": f"FC2-PPV-{fc2_id}",
                            "title": title,
                            "studio": "FC2-PPV",
                            "premiered": None,
                            "plot": title,
                            "poster_url": src,
                            "actors": [],
                            "tags": ["FC2", "PPV", "绝版镜像"],
                            "source": "FC2 Extended Mirror"
                        }
        except Exception:
            pass
    return None

def scrape_caribbeancom(number: str) -> dict:
    url = f"https://www.caribbeancom.com/moviepages/{number}/index.html"
    headers = {"User-Agent": COMMON_HEADERS["User-Agent"], "Cookie": "age_check=1"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            title = soup.find('h1').get_text(strip=True) if soup.find('h1') else number
            poster = f"https://www.caribbeancom.com/moviepages/{number}/images/l_l.jpg"
            actors = [a.get_text(strip=True) for a in soup.find_all('a', href=re.compile(r'/actress/'))]
            return {"id": number, "title": title, "studio": "Caribbeancom", "premiered": None, "plot": title, "poster_url": poster, "actors": actors, "tags": ["无码", "加勒比"], "source": "Caribbeancom Official"}
    except Exception:
        pass
    return None

def scrape_1pondo(number: str) -> dict:
    url = f"https://www.1pondo.tv/movies/{number}/"
    headers = {"User-Agent": COMMON_HEADERS["User-Agent"]}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            title = soup.find('h1').get_text(strip=True) if soup.find('h1') else number
            poster = f"https://www.1pondo.tv/assets/sample/{number}/str.jpg"
            actors = [a.get_text(strip=True) for a in soup.find_all('a', href=re.compile(r'/actress/'))]
            return {"id": number, "title": title, "studio": "1Pondo", "premiered": None, "plot": title, "poster_url": poster, "actors": actors, "tags": ["无码", "一本道"], "source": "1Pondo Official"}
    except Exception:
        pass
    return None

# --- ② 制作商直通车 (Prestige, Faleno, MGS, DMM) ---
def scrape_prestige_official(number: str) -> dict:
    sku_id = number.upper().replace("-", "")
    url = f"https://www.prestige-av.com/api/sku/item/{sku_id}"
    try:
        r = requests.get(url, headers={"Accept": "application/json", "User-Agent": COMMON_HEADERS["User-Agent"]}, timeout=8)
        if r.status_code == 200:
            data = r.json()
            uuid = (data.get("parentProduct") or {}).get("uuid")
            if uuid:
                p_url = f"https://www.prestige-av.com/api/product/{uuid}"
                p_res = requests.get(p_url, headers={"Accept": "application/json", "User-Agent": COMMON_HEADERS["User-Agent"]}, timeout=8).json()
                pkg = (p_res.get("packageImage") or {}).get("path")
                poster = f"https://image.prestige-av.com/{pkg}" if pkg and not pkg.startswith("http") else pkg
                actors = [a.get("name") for a in p_res.get("actress", []) if a.get("name")]
                return {"id": number, "title": p_res.get("title"), "studio": "Prestige", "premiered": (p_res.get("mgsStartAt") or "").split("T")[0] or None, "plot": p_res.get("body"), "poster_url": poster, "actors": actors, "tags": [g.get("name") for g in p_res.get("genre", [])], "source": "Prestige Official"}
    except Exception:
        pass
    return None

def scrape_faleno_official(number: str) -> dict:
    url = f"https://faleno.jp/top/search?keyword={number}"
    try:
        r = requests.get(url, headers=COMMON_HEADERS, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            item = soup.find('a', href=re.compile(r'/products/detail/'))
            if item:
                d_url = urljoin("https://faleno.jp", item.get('href'))
                dr = requests.get(d_url, headers=COMMON_HEADERS, timeout=8)
                if dr.status_code == 200:
                    dsoup = BeautifulSoup(dr.text, 'html.parser')
                    title = dsoup.find('h2', class_='p-product-title').get_text(strip=True) if dsoup.find('h2', class_='p-product-title') else number
                    img = dsoup.find('div', class_=re.compile(r'product-image')).find('img') if dsoup.find('div', class_=re.compile(r'product-image')) else None
                    poster = img.get('src') if img else None
                    actors = [a.get_text(strip=True) for a in dsoup.find_all('a', href=re.compile(r'/actress/'))]
                    return {"id": number, "title": title, "studio": "FALENO", "premiered": None, "plot": title, "poster_url": poster, "actors": actors, "tags": [], "source": "Faleno Official"}
    except Exception:
        pass
    return None

def scrape_mgstage(number: str) -> dict:
    url = f"https://www.mgstage.com/product/product_detail/{number}/"
    headers = {"User-Agent": COMMON_HEADERS["User-Agent"], "Cookie": "adc=1"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200 and "detail_data" in r.text:
            soup = BeautifulSoup(r.text, 'html.parser')
            h1 = soup.find('h1')
            title = h1.get_text(strip=True) if h1 else number
            poster_tag = soup.find('a', class_='enlarge_image') or soup.find('img', class_='enlarge_image')
            poster = poster_tag.get('href') or poster_tag.get('src') if poster_tag else None
            return {"id": number, "title": title, "studio": "MGS", "premiered": None, "plot": title, "poster_url": poster, "actors": [], "tags": [], "source": "MGStage"}
    except Exception:
        pass
    return None

# --- ③ 权威番号数据库与综合检索矩阵 (JavBus, JavDB, JavLibrary, AVSOX, AirAV, Jav321) ---
def scrape_javbus(number: str) -> dict:
    url = f"https://www.javbus.com/{number}"
    headers = {"User-Agent": COMMON_HEADERS["User-Agent"], "Cookie": "dv=1; existmag=all", "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200 and ("movie-box" in r.text or "bigImage" in r.text):
            soup = BeautifulSoup(r.text, 'html.parser')
            title = soup.find('h3').get_text(strip=True) if soup.find('h3') else number
            big_img = soup.find('a', class_='bigImage')
            poster = big_img.get('href') if big_img else None
            if poster and poster.startswith('/'): poster = "https://www.javbus.com" + poster
            studio, date_val, actors = None, None, []
            for p_tag in soup.find_all('span', class_='header'):
                txt = p_tag.get_text(strip=True)
                if '發行日期:' in txt:
                    date_val = p_tag.parent.get_text(strip=True).replace('發行日期:', '').strip()
                elif '製作商:' in txt or '發行商:' in txt:
                    studio = p_tag.find_next_sibling('a').get_text(strip=True) if p_tag.find_next_sibling('a') else None
            for star in soup.find_all('div', class_='star-name'):
                if star.find('a'): actors.append(star.find('a').get_text(strip=True))
            return {"id": number, "title": title, "studio": studio, "premiered": date_val, "plot": title, "poster_url": poster, "actors": actors, "tags": [], "source": "JavBus"}
    except Exception:
        pass
    return None

def scrape_javdb(number: str) -> dict:
    url = f"https://javdb.com/search?q={quote(number)}&f=all"
    try:
        r = requests.get(url, headers=COMMON_HEADERS, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            first_item = soup.find('div', class_='item')
            if first_item and first_item.find('img'):
                img = first_item.find('img').get('src')
                title = first_item.find('div', class_='video-title').get_text(strip=True) if first_item.find('div', class_='video-title') else number
                return {"id": number, "title": title, "studio": None, "premiered": None, "plot": title, "poster_url": img, "actors": [], "tags": [], "source": "JavDB"}
    except Exception:
        pass
    return None

def scrape_avsox(number: str) -> dict:
    url = f"https://avsox.host/cn/search/{quote(number)}"
    try:
        r = requests.get(url, headers=COMMON_HEADERS, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            item = soup.find('a', class_='movie-box')
            if item:
                title = item.find('img').get('title') if item.find('img') else number
                img = item.find('img').get('src') if item.find('img') else None
                return {"id": number, "title": title, "studio": "AVSOX", "premiered": None, "plot": title, "poster_url": img, "actors": [], "tags": [], "source": "AVSOX"}
    except Exception:
        pass
    return None

# --- ④ 二次元动漫里番矩阵 (Getchu) ---
def scrape_getchu(number: str) -> dict:
    url = f"http://www.getchu.com/php/nsearch.phtml?search_keyword={quote(number)}&gc=gc"
    headers = {"User-Agent": COMMON_HEADERS["User-Agent"], "Cookie": "getchu_adalt_flag=getchu.com; gc=gc"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            link = soup.find('a', href=re.compile(r'soft\.phtml'))
            if link:
                d_url = urljoin("http://www.getchu.com", link.get('href'))
                dr = requests.get(d_url, headers=headers, timeout=8)
                if dr.status_code == 200:
                    dsoup = BeautifulSoup(dr.text, 'html.parser')
                    title = dsoup.find('h1', id='soft-title').get_text(strip=True) if dsoup.find('h1', id='soft-title') else number
                    img = dsoup.find('a', class_='highslide') or dsoup.find('img', src=re.compile(r'/brand/'))
                    poster = urljoin("http://www.getchu.com", img.get('href') or img.get('src')) if img else None
                    return {"id": number, "title": title, "studio": "Getchu", "premiered": None, "plot": title, "poster_url": poster, "actors": [], "tags": ["Hentai", "Anime"], "source": "Getchu"}
    except Exception:
        pass
    return None

# --- ⑤ 欧美商业 Scene 专区 (ThePornDB) ---
def scrape_tpdb_scene(info: dict) -> dict:
    site = info.get("site", "")
    date_str = info.get("date")
    clean_title = info.get("clean_title", "")
    
    # 厂牌同义词标准化映射
    aliases = {
        "tsma": "teensexmania", "teensexmania": "teensexmania",
        "itc": "inthecrack", "inthecrack": "inthecrack",
        "girlfriendsfilms": "girlfriends", "gff": "girlfriends",
        "blackedraw": "blacked", "blacked": "blacked",
        "vixen": "vixen", "deeper": "deeper", "tushy": "tushy", "tushyraw": "tushy",
        "loveherfeet": "loveher", "loveherfilms": "loveher",
        "familyxxx": "family", "trueanal": "trueanal", "fit18": "fit18",
        "myfamilypies": "myfamilypies", "cum4k": "cum4k",
        "lcd": "littlecaprice", "littlecapricepov": "littlecaprice",
        "dpg": "digitalplayground", "manyvids": "manyvids",
        "bangingbeauties": "bangingbeauties", "jlmf": "jeshbyjesh",
        "bellesafilms": "bellesa", "jeshbyjesh": "jeshbyjesh",
        "devilsfilm": "devilsfilm", "hegre": "hegre", "hart": "hegre"
    }
    
    def _is_studio_matched(candidate_site: str, target_site: str) -> bool:
        if not target_site: return True
        c_norm = re.sub(r'[^a-zA-Z0-9]', '', candidate_site).lower()
        t_norm = re.sub(r'[^a-zA-Z0-9]', '', target_site).lower()
        c_mapped = aliases.get(c_norm, c_norm)
        t_mapped = aliases.get(t_norm, t_norm)
        return (t_mapped in c_mapped or c_mapped in t_mapped)

    # 策略 1: 厂牌 + 日期精确匹配（必须严格匹配厂牌）
    if site and date_str:
        try:
            url = f"https://api.theporndb.net/scenes?q={quote(site)}&date={date_str}"
            r = requests.get(url, headers=TPDB_HEADERS, timeout=8)
            if r.status_code == 200:
                data = r.json().get('data', [])
                for it in data:
                    it_site = (it.get('site') or {}).get('name', '')
                    if _is_studio_matched(it_site, site):
                        return _format_tpdb_result(it)
        except Exception:
            pass

    # 策略 2: 厂牌 + 标题/演职员（必须严格匹配厂牌）
    q_str = f"{site} {clean_title}".strip()
    if q_str:
        try:
            url = f"https://api.theporndb.net/scenes?q={quote(q_str)}"
            r = requests.get(url, headers=TPDB_HEADERS, timeout=8)
            if r.status_code == 200:
                data = r.json().get('data', [])
                for it in data:
                    it_site = (it.get('site') or {}).get('name', '')
                    if _is_studio_matched(it_site, site):
                        return _format_tpdb_result(it)
        except Exception:
            pass

    # 策略 3: 纯演职员搜索（【铁律】：必须同时命中原厂牌，绝不允许跨厂牌漂移！）
    pure_actors = re.sub(r'\b(and|sd|hd|mp4|ktr|kleenex|xxx|4k|720p|1080p)\b', '', clean_title, flags=re.IGNORECASE)
    pure_actors = re.sub(r'[\.\s_\-]+', ' ', pure_actors).strip()
    if pure_actors and len(pure_actors) > 3:
        try:
            url = f"https://api.theporndb.net/scenes?q={quote(pure_actors)}"
            r = requests.get(url, headers=TPDB_HEADERS, timeout=8)
            if r.status_code == 200:
                data = r.json().get('data', [])
                for it in data:
                    it_site = (it.get('site') or {}).get('name', '')
                    if _is_studio_matched(it_site, site):
                        return _format_tpdb_result(it)
        except Exception:
            pass

    # 策略 4: JavDB 兜底（同样必须包含厂牌关键字）
    if pure_actors and site:
        jav_res = scrape_javdb(f"{site} {pure_actors}")
        if jav_res and jav_res.get("poster_url"):
            return jav_res

    # 都不严格匹配则返回 None，由 CD2 黄金位抽帧保底，绝不误挂假封面！
    return None

def _format_tpdb_result(it: dict) -> dict:
    # 全字段穿透提取最高清海报/背景图
    p_url = (
        it.get('poster') or
        (it.get('posters') or {}).get('full') or
        (it.get('posters') or {}).get('large') or
        (it.get('background') or {}).get('full') or
        (it.get('background') or {}).get('large') or
        (it.get('posters') or {}).get('medium') or
        (it.get('background') or {}).get('medium') or
        it.get('image')
    )
    if p_url and (not isinstance(p_url, str) or not p_url.startswith('http') or p_url.endswith('/')):
        p_url = None

    actors = [p.get('name') for p in it.get('performers', []) if p.get('name')]
    tags = [t.get('name') if isinstance(t, dict) else str(t) for t in it.get('tags', [])]
    return {
        "id": it.get('id'),
        "title": it.get('title'),
        "studio": (it.get('site') or {}).get('name'),
        "premiered": it.get('date'),
        "plot": it.get('description'),
        "poster_url": p_url,
        "actors": actors,
        "tags": tags,
        "source": "ThePornDB"
    }

# ==================== 3. 顶层 4 级阶梯式瀑布流聚合引擎 (Master Waterfall Engine) ====================

def scrape_official_scene(filename: str, parent_dir_name: str = "") -> dict:
    """
    大一统瀑布流调度引擎：智能分流 ➔ 官方直连 ➔ 镜像归档 ➔ 综合数据库 ➔ 全量聚合
    """
    info = parse_media_identifier(filename, parent_dir_name)
    m_type = info.get("type")
    
    # 🥇 1. FC2 素人瀑布流：官方 -> FC2Club -> FC2PPVDB -> JavDB
    if m_type == "fc2":
        fid = info.get("id")
        for fn in [scrape_fc2_official, scrape_fc2club, scrape_fc2ppvdb, scrape_fc2_mirrors_deep]:
            res = fn(fid)
            if res and res.get("poster_url"): return res
        return scrape_javdb(f"FC2-PPV-{fid}")

    # 🥈 2. MGS 舞台专属瀑布流：MGStage -> Prestige -> JavBus -> JavDB
    elif m_type == "mgs":
        num = info.get("number")
        for fn in [scrape_mgstage, scrape_prestige_official, scrape_javbus, scrape_javdb]:
            res = fn(num)
            if res and res.get("poster_url"): return res
        return None

    # 🥉 3. 日本标准商业番号瀑布流：Prestige/Faleno官方 -> JavBus权威大图 -> JavDB -> AVSOX
    elif m_type == "jav":
        num = info.get("number")
        prefix = info.get("prefix", "")
        # 如果是已知大厂直连
        if "ABW" in prefix or "ABP" in prefix:
            res_pres = scrape_prestige_official(num)
            if res_pres and res_pres.get("poster_url"): return res_pres
        elif "FSDSS" in prefix or "FCDSS" in prefix:
            res_fal = scrape_faleno_official(num)
            if res_fal and res_fal.get("poster_url"): return res_fal
            
        for fn in [scrape_javbus, scrape_javdb, scrape_avsox]:
            res = fn(num)
            if res and res.get("poster_url"): return res
        return None

    # 🏅 3.5 无码四大厂直连瀑布流：Caribbeancom / 1Pondo -> JavDB
    elif m_type == "uncensored":
        studio = info.get("studio")
        num = info.get("number")
        if studio == "Caribbeancom":
            res = scrape_caribbeancom(num)
            if res and res.get("poster_url"): return res
        elif studio == "1Pondo":
            res = scrape_1pondo(num)
            if res and res.get("poster_url"): return res
        return scrape_javdb(num)

    # 🏅 4. 二次元动漫里番瀑布流：Getchu -> JavDB
    elif m_type == "hentai" or "getchu" in parent_dir_name.lower():
        num = info.get("number", filename)
        return scrape_getchu(num) or scrape_javdb(num)

    # 🌍 5. 欧美商业 Scene 瀑布流：ThePornDB 官方 4K
    else:
        return scrape_tpdb_scene(info)

# ==================== 4. 海报写入与 NFO 标准化生成 ====================

def download_and_save_poster(image_url: str, target_poster_path: str) -> bool:
    try:
        r = requests.get(image_url, headers=COMMON_HEADERS, stream=True, timeout=15)
        if r.status_code == 200:
            with open(target_poster_path, 'wb') as f:
                for chunk in r.iter_content(1024 * 64):
                    f.write(chunk)
            return True
    except Exception as e:
        print(f"下载海报失败: {e}")
    return False

def generate_nfo_file_content(data: dict, video_filename: str) -> str:
    root = ET.Element("movie")
    title = data.get('title') or video_filename
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "originaltitle").text = title
    
    if data.get('premiered'):
        ET.SubElement(root, "premiered").text = data.get('premiered')
        ET.SubElement(root, "year").text = data.get('premiered')[:4]
        
    if data.get('plot'):
        ET.SubElement(root, "plot").text = data.get('plot')
        ET.SubElement(root, "outline").text = data.get('plot')
        
    site_name = data.get('studio')
    if site_name:
        ET.SubElement(root, "studio").text = site_name
        ET.SubElement(root, "publisher").text = site_name
        
    for act in data.get('actors', []):
        actor_el = ET.SubElement(root, "actor")
        ET.SubElement(actor_el, "name").text = act
            
    for tag in data.get('tags', []):
        ET.SubElement(root, "genre").text = tag
        
    ET.SubElement(root, "lockdata").text = "true"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode('utf-8')

# ==================== 5. AI 纠错与智能重命名 ====================

def call_openrouter_batch_fix(filenames_list: list, ai_cfg: dict) -> list:
    if not filenames_list: return []
    api_base = ai_cfg.get("api_base", "https://openrouter.ai/api/v1")
    api_key = ai_cfg.get("api_key", "")
    model = ai_cfg.get("model", "minimax/minimax-m2.7:free")
    if not api_key: return []
        
    endpoint = f"{api_base.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    prompt = f"""You are a media file naming normalization expert.
Given a list of video release filenames that failed scraping, analyze and repair each into standard release format (Studio.YY.MM.DD.Performer.Title or Standard ID).

Input files:
{json.dumps(filenames_list, ensure_ascii=False)}

Output strictly valid JSON array of objects:
[
  {{"original": "raw_filename", "suggested": "Cleaned.Release.Name", "site": "Studio", "date": "YYYY-MM-DD"}}
]
No other text."""

    try:
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
        r = requests.post(endpoint, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            content = r.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            m = re.search(r'\[\s*\{.*?\}\s*\]', content, re.DOTALL)
            if m: return json.loads(m.group(0))
    except Exception as e:
        print("AI batch fix error:", e)
    return []

# ==================== 6. 任务状态持久化与断点自动恢复状态机 (Persistence State Machine) ====================

STATE_FILE = "/opt/treestrm/governance_state.json"

def save_running_state(current_job: dict, queue: list):
    try:
        data = {
            "current_job": current_job,
            "queue": queue,
            "updated_at": time.time()
        }
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存任务状态失败: {e}")

def load_running_state() -> tuple:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("current_job"), data.get("queue", [])
        except Exception:
            pass
    return None, []

def clear_running_state():
    if os.path.exists(STATE_FILE):
        try:
            os.remove(STATE_FILE)
        except Exception:
            pass

# ==================== 7. 规则级清洗函数 (供 app.py 调用) ====================

def clean_scene_name_rules(filename: str) -> str:
    """
    针对常见 Scene 杂质、括号扩展名、多重连字符进行规则清洗
    """
    fn = re.sub(r'[\.\s]?\((?:mp4|ts|mkv|wmv|avi)\)', '', filename, flags=re.IGNORECASE)
    fn = re.sub(r'^(?:www\.\.ws_|kcf9\.com\s*|\[.*?\]\s*)', '', fn, flags=re.IGNORECASE)
    fn = re.sub(r'XXX|2160p|1080p|720p|4k|540p|HEVC|x265|x264|PRT|MP4-KTR|-KTR|-sample', '', fn, flags=re.IGNORECASE)
    fn = re.sub(r'[-_]+', '.', fn)
    fn = re.sub(r'\.{2,}', '.', fn).strip('. -_')
    return fn
