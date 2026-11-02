"""知识库与游戏实际目录的一致性。

数据来源：1920x1080 全屏录屏的交易行左侧目录，逐帧 OCR 读出 13 项
（`docs/auto-buy-design.md` §4.3）。每项旁边的赛季标签是游戏自己画的，
不是我们推断的 —— 所以这组断言钉的是「bricks.json 有没有把砖皮记到错的赛季」。

赛季记错的后果不是显示不好看：`identify()` 的 `current` / 往期判定会跟着错，
抢购时会把往期砖皮当成当期、或者反过来，直接决定买不买。
"""
import pytest

from core import bricks

# (目录原文, 游戏显示的赛季)
CATALOG = [
    ("MK4 冲锋枪 疾光魅影", "S11"),
    ("AKM 突击步枪 群星", "S10"),
    ("KC17 突击步枪 造物纪元", "S9"),
    ("MP7 冲锋枪 电玩高手S2", "S8"),
    ("M250 通用机枪 电玩高手S2", "S8"),
    ("AS Val 突击步枪 悬赏令", "S7"),
    ("M7 战斗步枪 棱镜攻势S2", "S6"),
    ("AUG 突击步枪 气象感应", "S5"),
    ("腾龙 突击步枪 气象感应", "S5"),
    ("QBZ95-1 突击步枪 王牌之剑", "S4"),
    ("K416 突击步枪 命运", "S4"),
    ("SCAR-H 战斗步枪 电玩高手", "S3"),
    ("Vector 冲锋枪 美杜莎", "S2"),
]


@pytest.mark.parametrize("text,season", CATALOG)
def test_catalog_entry_maps_to_the_season_the_game_shows(text, season):
    got = bricks.identify(text)
    assert got["ok"], f"{text} 在知识库里查不到"
    best = got["options"][0]
    assert best["season"] == season, (
        f"{text} 游戏里标 {season}，知识库记成 {best['season']} {best['season_name']}"
    )


@pytest.mark.parametrize("text,season", CATALOG)
def test_catalog_entry_is_a_strong_match(text, season):
    """皮肤名必须命中，不能只靠枪型号猜出来。

    `identify()` 对「只认出枪型」的匹配标 `weak=True`。目录里每一项都写着
    皮肤名，所以这 13 项一条都不该是弱匹配；有弱匹配说明别名表漏了写法。
    """
    got = bricks.identify(text)
    assert not got["weak"], f"{text} 只认出枪型，皮肤名没命中"


def test_catalog_is_sorted_newest_first():
    """目录按赛季倒序排（S11 → S2）。

    这条决定了「滚动查找」能不能用赛季号估算目标在第几屏 ——
    顺序变了，按序号跳转就会滚过头。
    """
    order = [int(s[1:]) for _, s in CATALOG]
    assert order == sorted(order, reverse=True)
