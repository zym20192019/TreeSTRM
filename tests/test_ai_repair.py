from pathlib import Path
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.ai_repair import (
    AI_PENDING_TAG,
    apply_ai_result,
    find_pending_nfos,
    mark_ledger_repaired,
    repair_pending_nfos,
)

FALLBACK_NFO = """<?xml version='1.0' encoding='utf-8'?>
<movie>
  <title>圆圆 V (9)</title>
  <originaltitle>圆圆 V (9)</originaltitle>
  <sorttitle>圆圆 V (9)</sorttitle>
  <studio>Dongeuran</studio>
  <plot>圆圆写真视频第9号（AI提炼失败，待补全）</plot>
  <outline>圆圆写真视频第9号（AI提炼失败，待补全）</outline>
  <genre>写真</genre>
  <tag>写真</tag>
  <tag>AI待补</tag>
  <tag>圆圆</tag>
  <fileinfo><streamdetails><video><width>3840</width><height>2160</height></video></streamdetails></fileinfo>
</movie>"""


def _make_tree():
    out = tempfile.mkdtemp()
    d = os.path.join(out, "成人", "个人向", "圆圆")
    os.makedirs(d)
    for name in ("a.nfo", "b.nfo"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(FALLBACK_NFO)
    with open(os.path.join(d, "ok.nfo"), "w", encoding="utf-8") as f:
        f.write(FALLBACK_NFO.replace("<tag>AI待补</tag>", ""))
    return out, d


def test_find_pending_nfos_local():
    out, d = _make_tree()
    found = find_pending_nfos(os.path.join(out, "成人", "个人向"))
    assert [os.path.basename(p) for p in found] == ["a.nfo", "b.nfo"]


def test_apply_ai_result_keeps_streams_and_studio():
    out, d = _make_tree()
    p = os.path.join(d, "a.nfo")
    assert apply_ai_result(p, {"clean_title": "圆圆 写真 V9", "tags": ["韩系", "写真"], "summary": "新简介"})
    root = ET.parse(p).getroot()
    assert root.findtext("title") == "圆圆 写真 V9" and root.findtext("sorttitle") == "圆圆 写真 V9"
    assert root.findtext("plot") == "新简介" and root.findtext("outline") == "新简介"
    assert root.findtext("studio") == "Dongeuran" and root.findtext("genre") == "写真"
    assert [t.text for t in root.findall("tag")] == ["韩系", "写真", "圆圆"]
    assert root.findtext("fileinfo/streamdetails/video/width") == "3840"
    assert AI_PENDING_TAG not in open(p, encoding="utf-8").read()


def test_apply_ai_result_without_summary_strips_failure_suffix():
    out, d = _make_tree()
    p = os.path.join(d, "a.nfo")
    assert apply_ai_result(p, {"clean_title": "标题"})
    assert ET.parse(p).getroot().findtext("plot") == "圆圆写真视频第9号"


def test_apply_ai_result_rejects_empty_title():
    out, d = _make_tree()
    p = os.path.join(d, "a.nfo")
    assert not apply_ai_result(p, {"clean_title": "  "})
    assert AI_PENDING_TAG in open(p, encoding="utf-8").read()


def test_repair_pending_nfos_batches_and_partial_failure():
    out, d = _make_tree()
    calls = []

    def fake_ai(rels, folder_rel):
        calls.append((list(rels), folder_rel))
        return {rels[0]: {"clean_title": "只修第一个"}}

    nfos = find_pending_nfos(os.path.join(out, "成人"))
    stats = repair_pending_nfos(nfos, out, fake_ai, batch_size=40, log=lambda m: None)
    assert calls == [(["成人/个人向/圆圆/a.strm", "成人/个人向/圆圆/b.strm"], "成人/个人向/圆圆")]
    assert stats["found"] == 2 and stats["repaired"] == 1 and stats["failed"] == 1
    assert [os.path.basename(p) for p in find_pending_nfos(os.path.join(out, "成人"))] == ["b.nfo"]


def test_mark_ledger_repaired_only_fully_repaired_batches():
    ledger = os.path.join(tempfile.mkdtemp(), "ledger.json")
    records = [
        {"files": ["x/a.strm", "x/b.strm"], "status": "pending_repair"},
        {"files": ["x/a.strm", "x/c.strm"], "status": "pending_repair"},
        {"files": ["x/a.strm"], "status": "repaired"},
    ]
    with open(ledger, "w", encoding="utf-8") as f:
        json.dump(records, f)
    assert mark_ledger_repaired(ledger, {"x/a.strm", "x/b.strm"}) == 1
    after = json.load(open(ledger, encoding="utf-8"))
    assert [r["status"] for r in after] == ["repaired", "pending_repair", "repaired"]


def test_apply_ai_result_handles_name_at_length_limit():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "长" * 83 + "x.nfo")  # 83*3+1+4 = 254 字节，再加 ".tmp" 就超过 255
    with open(p, "w", encoding="utf-8") as f:
        f.write(FALLBACK_NFO)
    assert apply_ai_result(p, {"clean_title": "长名字"})
    assert ET.parse(p).getroot().findtext("title") == "长名字"
    assert os.listdir(d) == [os.path.basename(p)]
