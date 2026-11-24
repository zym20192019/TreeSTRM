import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.reconciler import sync_strms_pure_1to1


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


def test_cleanup_stays_inside_rule_dir():
    """两条规则共用输出目录：同步「短剧」不能删「成人」的 STRM，反之亦然。"""
    with tempfile.TemporaryDirectory() as out:
        adult = [os.path.join(out, "成人", "分类", f"{i}.strm") for i in range(600)]
        for p in adult:
            _touch(p)
        stale = os.path.join(out, "短剧", "旧剧", "旧剧 S01E01.strm")
        _touch(stale)
        vc, vs, vd = sync_strms_pure_1to1(["短剧/新剧/新剧 S01E01.mp4"], [], out, "http://x/d/115", scope="短剧")
        assert (vc, vd) == (1, 1)
        assert not os.path.exists(stale)
        assert all(os.path.exists(p) for p in adult)
        assert os.path.exists(os.path.join(out, "短剧", "新剧", "新剧 S01E01.strm"))

        # 反向：成人规则只认得自己的 600 个，短剧的不能动
        vc, vs, vd = sync_strms_pure_1to1([f"成人/分类/{i}.mp4" for i in range(600)], [], out, "http://x/d/115", scope="成人")
        assert vd == 0
        assert os.path.exists(os.path.join(out, "短剧", "新剧", "新剧 S01E01.strm"))


def test_scope_dir_missing_is_first_sync():
    with tempfile.TemporaryDirectory() as out:
        vc, vs, vd = sync_strms_pure_1to1(["短剧/a/a S01E01.mp4"], [], out, "http://x/d/115", scope="短剧")
        assert (vc, vs, vd) == (1, 0, 0)


if __name__ == "__main__":
    test_cleanup_stays_inside_rule_dir()
    test_scope_dir_missing_is_first_sync()
    print("ok")
