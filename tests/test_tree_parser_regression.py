from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.reconciler import _tree_entry_name, _tree_line_level, parse_tree_1to1


def test_tree_prefix_does_not_eat_real_leading_hyphen():
    assert _tree_entry_name("| | |-普通.mp4") == "普通.mp4"
    assert _tree_entry_name("| | |--猎奇.mp4") == "-猎奇.mp4"
    assert _tree_entry_name("| | |-GirlsDelta.mp4") == "GirlsDelta.mp4"
    assert _tree_entry_name("| | |-—破折号开头.mp4") == "—破折号开头.mp4"


def test_tree_prefix_preserves_other_name_characters():
    assert _tree_entry_name("| | |-  空格开头.mp4") == "  空格开头.mp4"
    assert _tree_entry_name("| | |-- -双空格横杠.mp4") == "- -双空格横杠.mp4"
    assert _tree_entry_name("| | |-尾部空格目录 ") == "尾部空格目录 "
    assert _tree_entry_name("| | |-a|b.mp4") == "a|b.mp4"


def test_root_line_and_levels():
    assert _tree_entry_name("\ufeff|——根目录") == "根目录"
    assert _tree_line_level("\ufeff|——根目录") == 1
    assert _tree_line_level("| | |-a|b.mp4") == 3


def test_parse_tree_keeps_whitespace_and_pipes():
    tree = "\ufeff|——根目录\n| |-成人\n| | |-咕姆 \n| | | |-1.mp4\n| | |-a|b.mp4\n| | |--x.mp4\n"
    videos, _, _ = parse_tree_1to1(tree, target_dir_name="成人")
    assert sorted(videos) == sorted(["成人/咕姆 /1.mp4", "成人/a|b.mp4", "成人/-x.mp4"])
