"""品阶目录（赛季 × 四个品阶）的查询与定位。

这是「我要抢某一砖」的知识底座：
  · bricks.catalog()  —— 整个目录
  · bricks.skins_of()  —— 按赛季/系列/品阶过滤
  · bricks.find_skin()  —— 按名字模糊找（OCR 认不准字，必须模糊）

名字比对必须容忍 OCR 的系统性噪声：游戏用花体字，「彩」会被认成
「采/未/米」，「白」会认成「自」，武器型号里的「-」会认成「·」。
所以比对前要归一化（去分隔符、去空白），再用相似度。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bricks, ocr  # noqa: E402


def test_catalog_has_seasons_and_tiers():
    cat = bricks.catalog()
    assert cat, "目录是空的"
    tiers = {row["tier"] for row in cat}
    assert "传说" in tiers and "史诗" in tiers
    assert "稀有" in tiers and "普通" in tiers


def test_every_skin_carries_a_series_and_a_weapon():
    for row in bricks.catalog():
        assert row["season"], row
        assert row["tier"], row
        assert row["name"], row
        assert row["weapon"], f"{row['name']} 没有武器型号"


def test_skins_of_filters_by_season_and_tier():
    rows = bricks.catalog()
    if not rows:
        return
    season = rows[0]["season"]
    tier = rows[0]["tier"]
    got = bricks.skins_of(season=season, tier=tier)
    assert got
    assert all(r["season"] == season and r["tier"] == tier for r in got)


def test_find_skin_is_fuzzy_about_ocr_noise():
    """名字被 OCR 认错一个字也要找得到 —— 比对前必须归一化。"""
    rows = bricks.catalog()
    if not rows:
        return
    target = rows[0]
    # 模拟 OCR 的典型噪声：分隔符认错、末尾一个字认错
    noisy = target["name"].replace("·", "．")
    for ch in "彩未米采":
        noisy = noisy.replace("彩", ch)
        hit = bricks.find_skin(noisy, tier=target["tier"])
        assert hit is not None, f"「{noisy}」没找得到"
        assert hit["season"] == target["season"]


def test_find_skin_returns_none_for_nonsense():
    assert bricks.find_skin("绝对不存在的皮肤名字xyzzy") is None


def test_normalize_drops_separators_and_spaces():
    assert ocr.normalize("· AK-12 突击步枪 · 水墨迷彩") == "ak12突击步枪水墨迷彩"


def test_find_skin_separates_subseries_from_brick_name():
    """子系列 ≠ 砖名 —— 定位时用的是砖名。

    游戏的「赛季下拉」里列的是砖名（棱镜攻势/美杜莎/命运王牌…），
    不是子系列（水墨迷彩/青纹/王牌之剑…）。子系列那一列是给界面显示用的，
    拿它去筛下拉会一条都找不到。
    """
    hit = bricks.find_skin("QBZ95-1突击步枪·王牌之剑")
    assert hit, "王牌之剑没找到"
    assert hit["series"] == "命运王牌", hit
    assert hit["brick"] == "命运王牌", hit

    sub = bricks.find_skin("G3战斗步枪·水墨迷彩")
    assert sub, "水墨迷彩没找到"
    assert sub["series"] == "水墨迷彩", sub
    assert sub["brick"] == "棱镜攻势", sub
    assert sub["brick"] != sub["series"], "子系列和砖名必须能分开"
