from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.meta_rules import clean_people, clean_studio, keep_sequence, unique_title


def test_clean_studio_drops_placeholders():
    assert clean_studio("未知") == ""
    assert clean_studio(" 未知工作室 ") == ""
    assert clean_studio("Unknown") == ""
    assert clean_studio("刑具不醒") == "刑具不醒"


def test_clean_people():
    assert clean_people(["刑具不醒", "未知", "刑具不醒", ""]) == ["刑具不醒"]
    assert clean_people("暖暖") == ["暖暖"]
    assert clean_people(None) == []


def test_keep_sequence_appends_missing_index():
    assert keep_sequence("刑具不醒-3", "刑具不醒-3 (4)") == "刑具不醒-3 (4)"
    assert keep_sequence("热舞猫 蕾蕾 第2期", "热舞猫 蕾蕾 第2期 (2)") == "热舞猫 蕾蕾 第2期 (2)"
    assert keep_sequence("全体工作人员盛赞的她的组装实力", "(4K) 조립실력 (2)") == "全体工作人员盛赞的她的组装实力 (2)"


def test_keep_sequence_leaves_good_titles():
    assert keep_sequence("合集 12", "钛合金TiTi NO.000 合集 (12)") == "合集 12"
    assert keep_sequence("高数3712 第7集", "高数3712 7") == "高数3712 第7集"
    assert keep_sequence("", "刑具不醒-3 (4)") == "刑具不醒-3 (4)"


def test_unique_title_falls_back_to_file_name():
    used = set()
    assert unique_title("九月合集片段", "9月 (1)", used) == "九月合集片段 (1)"
    assert unique_title("舞蹈老师直播精选", "舞蹈老师 20260921_001052_02", used) == "舞蹈老师直播精选"
    assert unique_title("舞蹈老师直播精选", "舞蹈老师 20260921_001052_03", used) == "舞蹈老师 20260921_001052_03"
