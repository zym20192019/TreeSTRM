
def clean_fc2_id(filename: str):
    m = re.search(r'(?:FC2[_-]?PPV[_-]?|FC2[_-]?)(\d{6,7})', filename, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r'(\d{6,7})', filename)
    if m2:
        return m2.group(1)
    return None

def scrape_fc2_official(fc2_id: str):
    url = f"https://adult.contents.fc2.com/article/{fc2_id}/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Cookie': 'age_check=1; adult=1; contents_adult=1;'
    }
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            from bs4 import BeautifulSoup
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
                if poster and poster.startswith('//'):
                    poster = 'https:' + poster
                    
            if not poster:
                sample_li = soup.find('ul', class_='items_article_SampleImagesArea')
                if sample_li and sample_li.find('a'):
                    poster = sample_li.find('a').get('href')
                    if poster and poster.startswith('//'):
                        poster = 'https:' + poster
                        
            return {
                'id': f"FC2-PPV-{fc2_id}",
                'title': title,
                'poster': poster,
                'site': {'name': 'FC2-PPV'},
                'description': title,
                'date': None,
                'performers': [],
                'tags': ['FC2', 'PPV', '无码']
            }
    except Exception as e:
        print(f"FC2 官方抓取异常 [{fc2_id}]:", e)
    return None

import os, sys, re, time, requests, threading
import xml.etree.ElementTree as ET

TPDB_TOKEN = "GYJTmJLvxL1XKiQ3XSJhImg9fWb3C6yVztbNyp8pf1cd2f25"
TPDB_HEADERS = {
    "Authorization": f"Bearer {TPDB_TOKEN}",
    "Accept": "application/json",
    "User-Agent": "TreeSTRM-Scraper/1.0"
}

def parse_scene_info(filename):
    fn = re.sub(r'\.[^.]+$', '', filename)
    fn = re.sub(r'\(mp4\)|\(mkv\)|\(wmv\)|\(avi\)|XXX|2160p|1080p|720p|4k', '', fn, flags=re.IGNORECASE).strip()
    
    m_date = re.search(r'(\d{2,4})[._-](\d{2})[._-](\d{2})', fn)
    date_str = None
    if m_date:
        y, m, d = m_date.groups()
        if len(y) == 2: y = "20" + y
        date_str = f"{y}-{m}-{d}"
        
    m_site = re.match(r'^([A-Za-z0-9]+)[._-]', fn)
    site = m_site.group(1) if m_site else ""
    
    clean = re.sub(r'^[A-Za-z0-9]+[._-]\d{2,4}[._-]\d{2}[._-]\d{2}[._-]?', '', fn)
    clean = re.sub(r'[._-]+', ' ', clean).strip()
    return site, date_str, clean

def scrape_official_scene(filename, parent_dir_name=""):
    # 0. 如果是 FC2 专区或包含 FC2 编号，优先走 FC2 官方源
    if 'fc2' in filename.lower() or 'fc2' in parent_dir_name.lower():
        fid = clean_fc2_id(filename)
        if fid:
            res_fc2 = scrape_fc2_official(fid)
            if res_fc2:
                return res_fc2
    site, date_str, clean_title = parse_scene_info(filename)
    if not site and parent_dir_name:
        site = parent_dir_name
        
    if site and date_str:
        try:
            url = f"https://api.theporndb.net/scenes?q={requests.utils.quote(site)}&date={date_str}"
            r = requests.get(url, headers=TPDB_HEADERS, timeout=8)
            if r.status_code == 200:
                data = r.json().get('data', [])
                if data:
                    for it in data:
                        it_site = (it.get('site') or {}).get('name', '').lower()
                        if site.lower() in it_site:
                            return it
                    return data[0]
        except Exception:
            pass

    q_str = f"{site} {clean_title}".strip()
    if q_str:
        try:
            url = f"https://api.theporndb.net/scenes?q={requests.utils.quote(q_str)}"
            r = requests.get(url, headers=TPDB_HEADERS, timeout=8)
            if r.status_code == 200:
                data = r.json().get('data', [])
                if data:
                    return data[0]
        except Exception:
            pass
            
    return None

def download_and_save_poster(image_url, target_poster_path):
    try:
        r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'}, stream=True, timeout=15)
        if r.status_code == 200:
            with open(target_poster_path, 'wb') as f:
                for chunk in r.iter_content(1024 * 64):
                    f.write(chunk)
            return True
    except Exception as e:
        print(f"下载官方海报失败: {e}")
    return False

def generate_nfo_file_content(scene_data, video_filename):
    root = ET.Element("movie")
    
    title = scene_data.get('title') or video_filename
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "originaltitle").text = title
    
    if scene_data.get('date'):
        ET.SubElement(root, "premiered").text = scene_data.get('date')
        ET.SubElement(root, "year").text = scene_data.get('date')[:4]
        
    if scene_data.get('description'):
        ET.SubElement(root, "plot").text = scene_data.get('description')
        ET.SubElement(root, "outline").text = scene_data.get('description')
        
    site_name = (scene_data.get('site') or {}).get('name')
    if site_name:
        ET.SubElement(root, "studio").text = site_name
        ET.SubElement(root, "publisher").text = site_name
        
    for perf in scene_data.get('performers', []):
        actor = ET.SubElement(root, "actor")
        ET.SubElement(actor, "name").text = perf.get('name')
        if perf.get('image'):
            ET.SubElement(actor, "thumb").text = perf.get('image')
            
    for tag in scene_data.get('tags', []):
        ET.SubElement(root, "genre").text = tag.get('name') if isinstance(tag, dict) else str(tag)
        
    ET.SubElement(root, "lockdata").text = "true"
    
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode('utf-8')


def clean_scene_name_rules(name: str) -> str:
    """第一道防线：基于规范模式清洗"""
    fn = re.sub(r'\.[^.]+$', '', name)
    fn = re.sub(r'\(mp4\)|\(mkv\)|\(wmv\)|\(avi\)|XXX|2160p|1080p|720p|4k', '', fn, flags=re.IGNORECASE).strip()
    tokens = [w.capitalize() for w in re.split(r'[._\s-]+', fn) if w]
    return ".".join(tokens)

def call_openrouter_batch_fix(filenames_list: list, ai_cfg: dict) -> list:
    """第二道防线：AI 智能批量语义纠错（两轮补全）"""
    if not filenames_list:
        return []
        
    api_base = ai_cfg.get("api_base", "https://openrouter.ai/api/v1")
    api_key = ai_cfg.get("api_key", "")
    model = ai_cfg.get("model", "minimax/minimax-m2.7:free")
    
    if not api_key:
        return []
        
    endpoint = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    prompt = f"""You are a media file naming normalization expert.
Given a list of video release filenames that failed scraping, analyze and repair each into standard release format (Studio.YY.MM.DD.Performer.Title).

Input files:
{json.dumps(filenames_list, ensure_ascii=False)}

Output strictly valid JSON array of objects:
[
  {{"original": "raw_filename", "suggested": "Cleaned.Release.Name", "site": "Studio", "date": "YYYY-MM-DD"}}
]
No other text."""

    try:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        }
        r = requests.post(endpoint, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            content = r.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            m = re.search(r'\[\s*\{.*?\}\s*\]', content, re.DOTALL)
            if m:
                return json.loads(m.group(0))
    except Exception as e:
        print("AI batch fix error:", e)
    return []

def rename_115_file(session, file_id: str, new_name: str) -> bool:
    """调用 115 官方 API 接口重命名云端文件"""
    try:
        url = "https://webapi.115.com/files/edit"
        data = {
            "fid": file_id,
            "file_name": new_name
        }
        r = session.post(url, data=data, timeout=10).json()
        return bool(r.get("state"))
    except Exception as e:
        print(f"115 重命名失败 [{file_id} ➔ {new_name}]:", e)
    return False
