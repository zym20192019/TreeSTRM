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
