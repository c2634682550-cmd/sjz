"""筛选表（core/skins_raw.csv）：每把枪在筛选里该点哪儿。

这张表是官方概率公示整理出来的 243 件，也是 bricks.json 的来源。它的用处
只有一个：告诉程序**筛选面板上该点哪个框**。所以这里钉的是「映射完整」：

  · 243 件一件不少，每件都能算出 赛季砖名 / 品阶第几框 / 枪种哪个框
  · 官方 8 种枪种并成面板上的 7 个框（突击步枪和战斗步枪都归「步枪」）
  · 普通品质是第 3 个框（0=传说 1=史诗 2=稀有 3=普通）
  · 每件皮肤所在的组合至少有 1 件 —— 这样按组合筛永远不会筛空
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bricks, skinfilter  # noqa: E402
from core.nav_col import NavColPanel  # noqa: E402


def test_the_table_covers_every_skin():
    """243 件全在，而且和知识库对得上（两边是同一份数据的两种形态）。"""
    r = skinfilter.verify(bricks.catalog())
    assert r["ok"] is True, r["problems"][:5]
    assert r["count"] == 243


def test_every_skin_maps_to_a_panel_slot():
    """每件皮肤都能落到面板上真实存在的格子。

    缺一个就意味着：这件皮肤永远筛不到，只能靠全库扫 —— 慢，而且
    说明表和面板对不上了。
    """
    slots = set(NavColPanel.GUN_ROWS)
    for row in skinfilter.load():
        assert row["gun"] in slots, f"{row['model']}-{row['name']}：{row['gun_raw']}"
        assert row["tier_index"] in (0, 1, 2, 3), row
        assert row["season_item"] in skinfilter.BRICKS, row


def test_eight_gun_types_collapse_into_seven_boxes():
    """官方 8 种枪种 → 面板 7 个框：突击步枪和战斗步枪都点「步枪」。

    这层映射做错的话，找「战斗步枪」的皮肤会去点一个不存在的框，
    或者错点成「冲锋枪」。
    """
    raw = {r["gun_raw"] for r in skinfilter.load()}
    assert len(raw) == 8, raw
    assert skinfilter.GUN_SLOTS["突击步枪"] == "步枪"
    assert skinfilter.GUN_SLOTS["战斗步枪"] == "步枪"
    assert skinfilter.GUN_SLOTS["射手步枪"] == "精准射手步枪"
    assert len({v for v in skinfilter.GUN_SLOTS.values()}) == 7


def test_plan_says_exactly_where_to_click():
    """拿一件具体的：M4A1-棱镜攻势 → 赛季选棱镜攻势、品阶第 0 框、枪种步枪。"""
    p = skinfilter.plan("M4A1突击步枪-棱镜攻势")
    assert p is not None
    assert p["season_item"] == "棱镜攻势"
    assert p["tier_index"] == 0 and p["tier"] == "传说"
    assert p["gun"] == "步枪" and p["gun_raw"] == "突击步枪"


def test_common_tier_is_the_fourth_box():
    """普通品质 → 第 3 个框（0 起算）。用户问的「普通品质能不能找到」就靠这条。"""
    commons = [r for r in skinfilter.load() if r["tier"] == "普通"]
    assert len(commons) == 108, len(commons)
    assert all(r["tier_index"] == 3 for r in commons)
    p = skinfilter.plan("QBZ95-1突击步枪-叠锋")      # 普通品阶
    assert p["tier_index"] == 3 and p["tier"] == "普通"


def test_every_combo_has_at_least_one_skin():
    """每件皮肤所在的「赛季+品阶+枪种」组合至少 1 件。

    这是「按组合筛不会筛空」的前提 —— 筛空会让游戏把面板变成居中浮层，
    坐标全变、点不动（core/skinfind.py 里有详细说明）。
    """
    combos = skinfilter.combos()
    for row in skinfilter.load():
        key = (row["season_item"], row["tier"], row["gun"])
        assert combos.get(key, 0) >= 1, f"{row['model']}-{row['name']} 的组合是空的：{key}"


def test_count_reports_how_many_are_in_a_combo():
    assert skinfilter.count("群星", "普通", "步枪") >= 1
    assert skinfilter.count("群星", "传说", "手枪") == 0      # 这个组合确实没有


def test_unknown_name_has_no_plan():
    assert skinfilter.plan("根本不存在的皮肤xyz") is None
    assert skinfilter.plan("") is None


def test_describe_is_human_readable():
    text = skinfilter.describe("M4A1突击步枪-棱镜攻势")
    assert "棱镜攻势" in text and "传说" in text and "步枪" in text


@pytest.mark.parametrize("season", sorted(skinfilter.SEASON_BRICK))
def test_every_season_has_a_brick_name(season):
    """11 个赛季每个都有对应的下拉项，且都在下拉列表里。"""
    assert skinfilter.SEASON_BRICK[season] in skinfilter.BRICKS
