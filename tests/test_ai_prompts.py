from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.ai_prompts import (GENRE_VOCAB, build_dir_prompt, build_fallback_prompt, build_item_prompt, category_of,
                               clean_tags, pick_genres)


def test_category_of():
    assert category_of("成人/舞蹈/精舞社/x") == "舞蹈"
    assert category_of("成人/抖音风裸舞弹力插") == "抖音风裸舞弹力插"
    assert category_of("") == ""


def test_clean_tags_drops_generic_category_and_names():
    tags = ["成人", "成人向", "#竖屏", "抖音风裸舞弹力插", "刑具不醒", "4K原画", "竖屏", "黑丝"]
    assert clean_tags(tags, category="抖音风裸舞弹力插", names=["刑具不醒"]) == ["竖屏", "黑丝"]


def test_clean_tags_limit():
    assert clean_tags([str(i) + "号" for i in range(10)], limit=3) == ["0号", "1号", "2号"]


def test_pick_genres_default_first_and_vocab_only():
    assert pick_genres("抖音风裸舞弹力插", ["成人舞蹈", "直播"]) == ["舞蹈", "直播"]
    assert pick_genres("个人向", "Cosplay/舞蹈") == ["Cosplay", "舞蹈"]
    assert pick_genres("系列", ["乱写的"]) == ["系列"]
    assert all(g in GENRE_VOCAB for g in pick_genres("写真", ["Cosplay", "私拍", "舞蹈"]))


def test_prompts_carry_context():
    d = build_dir_prompt("成人/直播/舞蹈老师", ["成人/直播/舞蹈老师/舞蹈老师 20260921_001052.strm"])
    assert "库名「直播」" in d and "默认分类是「直播」" in d and "舞蹈老师 20260921_001052.strm" in d
    p = build_item_prompt("成人/直播/舞蹈老师", ["a/舞蹈老师 1.strm"], {"studio": "舞蹈老师", "actors": ["舞蹈老师"], "common_tags": ["直播回放"]})
    assert "已知制作方：舞蹈老师" in p and "直播回放" in p and "1. 舞蹈老师 1.strm" in p
    assert "必须保留" in build_fallback_prompt(["1. 原路径: x -> 中文标题: y"])


def test_pick_genres_drops_uncensored_outside_av_libraries():
    from engine.ai_prompts import pick_genres
    assert pick_genres("cd", ["舞蹈", "无码"]) == ["舞蹈"]
    assert pick_genres("个人向", ["无码", "私拍"]) == ["私拍"]
    assert pick_genres("系列", ["无码"]) == ["无码"]
    assert pick_genres("无码合集", ["无码"]) == ["无码"]


def test_assemble_meta_ai_library_drops_uploader_as_actor():
    from engine.ai_prompts import assemble_meta
    ctx = {"studio": "黄花闺头", "actors": ["黄花闺头"], "genres": [], "common_tags": []}
    assert assemble_meta({}, ctx, "AI真人", "t")["actors"] == []
    assert assemble_meta({}, ctx, "个人向", "t")["actors"] == ["黄花闺头"]
