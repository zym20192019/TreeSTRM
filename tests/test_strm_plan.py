from pathlib import Path
import os
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.reconciler import plan_expected_strms, _strm_content

PREFIX = "http://host:5244/d/115"


def _plan(videos, existing_files=None):
    out = tempfile.mkdtemp()
    existing = set()
    for rel, content in (existing_files or {}).items():
        p = os.path.join(out, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        existing.add(p)
    exp, rep = plan_expected_strms(videos, out, PREFIX, existing)
    return {os.path.relpath(k, out): v for k, v in exp.items()}, {os.path.relpath(k, out) for k in rep}


def test_single_video_keeps_base_name():
    exp, rep = _plan(["成人/a/x.mp4"])
    assert exp == {"成人/a/x.strm": _strm_content(PREFIX, "成人/a/x.mp4")} and rep == set()


def test_same_base_new_group_prefers_mp4_and_adds_full_name_strm():
    exp, _ = _plan(["成人/a/x.mov", "成人/a/x.mp4", "成人/a/x.mp4"])
    assert exp["成人/a/x.strm"].endswith("/x.mp4")
    assert exp["成人/a/x.mov.strm"].endswith("/x.mov")
    assert len(exp) == 2


def test_same_base_keeps_existing_primary():
    existing = {"成人/a/x.strm": _strm_content(PREFIX, "成人/a/x.mov") + "\n"}
    exp, rep = _plan(["成人/a/x.mp4", "成人/a/x.mov"], existing)
    assert exp["成人/a/x.strm"].endswith("/x.mov")
    assert exp["成人/a/x.mp4.strm"].endswith("/x.mp4")
    assert rep == set()


def test_stale_primary_is_repaired():
    existing = {"成人/a/x.strm": _strm_content(PREFIX, "成人/a/x.avi")}
    exp, rep = _plan(["成人/a/x.mp4", "成人/a/x.mkv"], existing)
    assert exp["成人/a/x.strm"].endswith("/x.mp4") and rep == {"成人/a/x.strm"}


def test_full_name_strm_never_overrides_a_base_strm():
    exp, _ = _plan(["成人/a/x.mp4", "成人/a/x.mkv", "成人/a/x.mkv.mp4"])
    assert exp["成人/a/x.strm"].endswith("/x.mp4")
    assert exp["成人/a/x.mkv.strm"].endswith("/x.mkv.mp4")
