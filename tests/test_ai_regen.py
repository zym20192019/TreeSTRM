from pathlib import Path
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.ai_prompts import META_MARK_TAG, META_VERSION, assemble_meta, dir_context
from engine.ai_regen import list_regen_targets, regen_folder
from engine.nfo_builder import update_nfo_meta, write_nfo_file

OLD_NFO = """<?xml version="1.0" encoding="UTF-8" ?>
<movie>
  <title><![CDATA[Purple Bitch Leaks 394]]></title>
  <originaltitle><![CDATA[Purple Bitch Leaks 394]]></originaltitle>
  <sorttitle><![CDATA[Purple Bitch Leaks 394]]></sorttitle>
  <studio></studio>
  <plot><![CDATA[Purple Bitch系列泄露合集片段394。]]></plot>
  <outline><![CDATA[Purple Bitch系列泄露合集片段394。]]></outline>
  <genre>精选</genre>
  <tag>Purple Bitch</tag>
  <tag>Leaks</tag>
  <runtime>3</runtime>
  <fileinfo><streamdetails><video><width>1080</width><height>1920</height></video></streamdetails></fileinfo>
</movie>"""


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_dir_context_and_assemble_meta():
    ctx = dir_context({"studio": "未知", "actors": ["Purple Bitch"], "genres": ["欧美", "自拍"],
                       "common_tags": ["成人", "Cosplay"], "summary": "Purple Bitch 的自拍合集"})
    assert ctx["studio"] == "Purple Bitch" and ctx["actors"] == ["Purple Bitch"]
    meta = assemble_meta({"tags": ["Bowsette", "Purple Bitch"], "summary": ""}, ctx, "欧美", "T")
    assert meta["genres"] == ["欧美", "自拍"]
    assert meta["tags"] == ["Bowsette", "Cosplay"]
    assert meta["summary"] == "Purple Bitch 的自拍合集"
    failed = assemble_meta({"tags": []}, ctx, "欧美", "T", failed=True)
    assert "AI待补" in failed["tags"] and failed["summary"].endswith("（AI提炼失败，待补全）")
    assert dir_context(None) == {}


def test_update_nfo_meta_keeps_streams(tmp_path):
    p = _write(tmp_path / "a.nfo", OLD_NFO)
    assert update_nfo_meta(str(p), {"title": "Purple Bitch 394", "studio": "Purple Bitch", "actors": ["Purple Bitch"],
                                    "genres": ["欧美"], "tags": ["Cosplay"], "summary": "简介"})
    r = ET.parse(p).getroot()
    assert r.findtext("title") == r.findtext("sorttitle") == "Purple Bitch 394"
    assert r.findtext("studio") == "Purple Bitch"
    assert [g.text for g in r.findall("genre")] == ["欧美"]
    assert [t.text for t in r.findall("tag")] == ["Cosplay"]
    assert [a.findtext("name") for a in r.findall("actor")] == ["Purple Bitch"]
    assert r.findtext(META_MARK_TAG) == META_VERSION
    assert r.findtext("runtime") == "3" and r.findtext("fileinfo/streamdetails/video/height") == "1920"


def test_update_nfo_meta_drops_empty_studio_and_plot(tmp_path):
    p = _write(tmp_path / "a.nfo", OLD_NFO)
    assert update_nfo_meta(str(p), {"title": "x", "studio": "", "summary": "", "genres": [], "tags": [], "actors": []})
    r = ET.parse(p).getroot()
    assert r.find("studio") is None and r.find("plot") is None


def test_write_nfo_file_has_marker(tmp_path):
    p = tmp_path / "n.nfo"
    write_nfo_file(str(p), {"title": "t", "tags": [], "genres": []})
    assert ET.parse(p).getroot().findtext(META_MARK_TAG) == META_VERSION


def test_list_and_regen_folder(tmp_path):
    root = tmp_path / "TreeStrms"
    d = root / "成人" / "欧美" / "purple bitch"
    d.mkdir(parents=True)
    for i in (1, 2, 3):
        _write(d / f"pb ({i}).strm", "http://x")
        _write(d / f"pb ({i}).nfo", OLD_NFO)
    _write(d / "scraped.strm", "http://x")
    _write(d / "scraped.nfo", "<movie><title>s</title><scraper>mdc-ng</scraper></movie>")
    _write(d / "orphan.nfo", OLD_NFO)  # 没有 .strm
    groups = list_regen_targets(str(root / "成人" / "欧美"))
    assert list(groups) == [str(d)]
    assert sorted(Path(p).name for p in groups[str(d)]) == ["pb (1).nfo", "pb (2).nfo", "pb (3).nfo"]

    def dir_call(rel, rels):
        return {"studio": "Purple Bitch", "actors": ["Purple Bitch"], "genres": ["自拍"], "common_tags": ["Cosplay"], "summary": "s"}

    def item_call(rels, folder_rel, ctx):
        assert ctx["studio"] == "Purple Bitch"
        # 第 3 个失败；前两个 AI 给了同名标题
        return {r: {"clean_title": "Purple Bitch Leaks", "tags": ["泳池"], "summary": ""} for r in rels if "(3)" not in r}

    st = regen_folder(str(d), groups[str(d)], str(root), dir_call, item_call, batch_size=2, log=lambda m: None)
    assert st == {"done": 2, "failed": 1, "skipped_dir": 0}
    t1 = ET.parse(d / "pb (1).nfo").getroot().findtext("title")
    t2 = ET.parse(d / "pb (2).nfo").getroot().findtext("title")
    assert t1 == "Purple Bitch Leaks (1)" and t2 == "Purple Bitch Leaks (2)"
    assert ET.parse(d / "pb (3).nfo").getroot().findtext("title") == "Purple Bitch Leaks 394"
    # 重写过的带版本标记，再列一次只剩失败那个
    again = list_regen_targets(str(root / "成人" / "欧美"))
    assert [Path(p).name for p in again[str(d)]] == ["pb (3).nfo"]


def test_regen_folder_skips_when_dir_ai_fails(tmp_path):
    d = tmp_path / "成人" / "欧美" / "x"
    d.mkdir(parents=True)
    nfo = _write(d / "a.nfo", OLD_NFO)
    st = regen_folder(str(d), [str(nfo)], str(tmp_path), lambda *a: None, lambda *a: {}, log=lambda m: None)
    assert st == {"done": 0, "failed": 1, "skipped_dir": 1}
    assert ET.parse(nfo).getroot().findtext("title") == "Purple Bitch Leaks 394"


def test_regen_folder_with_pool(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    d = tmp_path / "成人" / "欧美" / "y"
    d.mkdir(parents=True)
    nfos = [str(_write(d / f"v{i}.nfo", OLD_NFO)) for i in range(7)]
    seen = []

    def item_call(rels, folder_rel, ctx):
        seen.append(len(rels))
        return {r: {"clean_title": os.path.basename(r)[:-5], "tags": [], "summary": "x"} for r in rels}

    with ThreadPoolExecutor(3) as pool, ThreadPoolExecutor(4) as wp:
        st = regen_folder(str(d), nfos, str(tmp_path), lambda *a: {"studio": "S"}, item_call, batch_size=3,
                          log=lambda m: None, pool=pool, write_pool=wp)
    assert st == {"done": 7, "failed": 0, "skipped_dir": 0} and sorted(seen) == [1, 3, 3]
    assert ET.parse(d / "v6.nfo").getroot().findtext("title") == "v6"


def test_assemble_meta_item_actors_and_genre_tags():
    ctx = dir_context({"studio": "Purple Bitch", "actors": ["Purple Bitch"], "genres": ["Cosplay"], "common_tags": ["剧情"]})
    meta = assemble_meta({"tags": ["Cosplay", "Jinx", "剧情"], "actors": ["Mia Bandini", "Purple Bitch"]}, ctx, "欧美", "T")
    assert meta["actors"] == ["Purple Bitch", "Mia Bandini"]
    assert meta["genres"] == ["欧美", "Cosplay"]
    assert "Cosplay" not in meta["tags"] and "Jinx" in meta["tags"]
    assert assemble_meta({"tags": []}, ctx, "欧美", "T")["actors"] == ["Purple Bitch"]
