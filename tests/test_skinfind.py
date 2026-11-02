"""找皮肤的契约：筛 → 滚 → 认 → 逐级放宽。

这里用一台「假游戏」：一个虚拟列表 + 一屏 7 张卡 + 一格滚轮走一张卡
（步长是 2026-11-02 真机量出来的）。断言的都是行为，不碰真界面：

  · 目标在第一屏 → 不滚动直接点中
  · 目标在第 N 屏 → 滚过去找到
  · 筛选条件跟实际不符 → 逐级放宽，最后全库扫，仍要找到
  · 列表名被截断 → 靠点开读标题确认，不靠列表名
  · 完全不相关的卡 → 一次都不点（省时间，也避免误选）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ocr  # noqa: E402
from core.nav_col import NavColPanel  # noqa: E402
from core.skinfind import SkinFinder  # noqa: E402

PER_SCREEN = 7          # 一屏 7 张卡
ROW_H = 114             # 卡间距（真机实测）
TIERS = ("传说", "史诗", "稀有", "普通")


class FakeGame:
    """一台假游戏：虚拟列表 + 筛选 + 滚动 + 点开读标题。"""

    rank_skin_cards = NavColPanel.rank_skin_cards      # 复用真实现

    ALL_GUNS = ("步枪", "冲锋枪", "霰弹枪", "轻机枪",
                "精准射手步枪", "狙击步枪", "手枪")

    def __init__(self, catalog, season=None):
        self.catalog = catalog          # [{list_text, title, season, tier, gun}]
        # season：开局就筛在某个赛季上（模拟用户/上次运行留下的筛选）
        self.season = season
        self.tiers = set(TIERS)         # 勾着哪些品阶
        self.guns = set(self.ALL_GUNS)  # 勾着哪些枪种
        self.offset = 0
        self.opened = None
        self.clicks = []
        self.scrolls = []
        self.filters_applied = []

    # ---- 筛选 ----
    def goto_collection(self):
        pass

    def col_filter(self):
        pass

    def col_filter_reset(self):
        self.season = None
        self.tiers = set(TIERS)
        self.guns = set(self.ALL_GUNS)
        self.offset = 0

    def col_pick_season(self, name):
        # 「全部赛季」是下拉里的第一项，等价于不筛赛季
        self.season = None if name in (None, "", "全部赛季") else name
        self.offset = 0

    def set_tier(self, index, only=True):
        if only:
            self.tiers = {TIERS[index]}
        else:
            self.tiers.add(TIERS[index])
        self.offset = 0

    def set_gun(self, gun=None):
        self.guns = set(self.ALL_GUNS) if gun is None else {gun}
        self.offset = 0

    def col_check(self, group, index, want=True):
        if group == "rarity":
            (self.tiers.add if want else self.tiers.discard)(TIERS[index])
        return True

    def col_filter_confirm(self):
        self.filters_applied.append({"season": self.season,
                                     "tiers": sorted(self.tiers),
                                     "guns": sorted(self.guns)})
        self.offset = 0

    def _wait_skin_list(self, before=None, timeout=1.0):
        pass

    # ---- 列表 ----
    def _rows(self):
        return [c for c in self.catalog
                if (not self.season or c["season"] == self.season)
                and c["tier"] in self.tiers
                and c["gun"] in self.guns]

    def skin_items(self):
        rows = self._rows()
        window = rows[self.offset:self.offset + PER_SCREEN]
        return [(c["list_text"], 300 + i * ROW_H) for i, c in enumerate(window)]

    def skin_scroll(self, notches):
        self.scrolls.append(notches)
        total = len(self._rows())
        self.offset = max(0, min(self.offset + notches, max(0, total - 1)))

    def skin_scroll_top(self, max_notches=40):
        self.offset = 0

    # ---- 点击与标题 ----
    def click(self, pt, label=None, expect_change=True):
        x, y = pt
        self.clicks.append((int(x), int(y)))
        rows = self._rows()
        idx = int(round((y - 300) / float(ROW_H)))
        if 0 <= idx < PER_SCREEN and self.offset + idx < len(rows):
            self.opened = rows[self.offset + idx]
        return True

    def skin_title(self):
        return self.opened["title"] if self.opened else ""

    def say(self, msg, level="info"):
        pass


def _skin(name, season="群星", tier="普通", gun="步枪", list_text=None):
    return {"list_text": list_text or name, "title": name,
            "season": season, "tier": tier, "gun": gun}


def test_finds_a_skin_visible_on_the_first_screen():
    """目标就在第一屏：不滚动，直接点中。"""
    game = FakeGame([_skin("MK4冲锋枪-疾光魅影"), _skin("AKM突击步枪-群星")])
    res = SkinFinder(game).find("AKM突击步枪-群星",
                                season="群星", tier="普通", gun="步枪")
    assert res["ok"] is True
    assert res["title"] == "AKM突击步枪-群星"
    # 当前屏就有 → 走快路径，连筛选都不碰
    assert res["plan"] == "当前屏"
    assert res.get("quick") is True
    assert game.scrolls == [], f"不该滚动：{game.scrolls}"
    assert game.filters_applied == [], "不该动筛选"


def test_scrolls_down_until_the_target_appears():
    """目标在第 3 屏：要滚过去找到，而且点的是正确那张卡。"""
    catalog = [_skin(f"QBZ95-1突击步枪-{i}") for i in range(20)]
    catalog[15] = _skin("K416突击步枪-命运")
    game = FakeGame(catalog)
    res = SkinFinder(game, step=6).find("K416突击步枪-命运",
                                        season="群星", tier="普通", gun="步枪")
    assert res["ok"] is True
    assert res["title"] == "K416突击步枪-命运"
    assert game.scrolls, "应该滚动过"
    assert res["screens"] > 1
    # 点开的位置必须真的对应那张卡
    rows = game._rows()
    idx = int(round((res["y"] - 300) / float(ROW_H)))
    assert rows[game.offset + idx]["title"] == "K416突击步枪-命运"


def test_stops_with_a_clear_message_when_the_filter_empties_the_list():
    """筛出空列表时必须立刻停手并说清楚，不能在那儿瞎点。

    游戏在列表被筛空的时候会把筛选面板变成屏幕中央的浮层，坐标全变 ——
    这时候继续按贴边坐标点，每一击都落空，看起来像「卡住了」。
    """
    # 表里说「棱镜攻势·传说·步枪」有货，但这台假游戏里一件都没有
    game = FakeGame([_skin("SCAR-H战斗步枪-幻夜", season="棱镜攻势",
                           tier="稀有", gun="步枪")], season="群星")
    res = SkinFinder(game).find("SCAR-H战斗步枪-幻夜",
                                season="棱镜攻势", tier="传说", gun="步枪")
    assert res["ok"] is False
    assert "筛空" in res["msg"], res["msg"]


def test_falls_back_to_the_whole_library_when_the_combo_has_nothing():
    """精确组合筛出来没有这件（但列表非空）→ 逐级放宽，最后全库扫到。"""
    # 目标在「棱镜攻势」，但按「群星·普通·步枪」筛也能筛出别的东西
    game = FakeGame([_skin("M4A1突击步枪-棱镜攻势", season="棱镜攻势",
                           tier="传说", gun="步枪"),
                     _skin("QBZ95-1突击步枪-叠锋", season="群星",
                           tier="普通", gun="步枪")],
                    season="群星")
    res = SkinFinder(game).find("M4A1突击步枪-棱镜攻势",
                                season="群星", tier="普通", gun="步枪")
    assert res["ok"] is True, res
    assert res["plan"] == "全库扫描", res["plan"]
    assert len(game.filters_applied) >= 2, "应该逐级放宽"


def test_reports_not_found_after_scanning_everything():
    """真的没有这件：要老实说没找到，并给出翻了多少屏。"""
    game = FakeGame([_skin("MK4冲锋枪-疾光魅影")])
    res = SkinFinder(game).find("根本没有这个皮肤xyz")
    assert res["ok"] is False
    assert res["screens"] >= 1
    assert "找不到" in (res.get("msg") or "") or "没找到" in (res.get("msg") or "")


def test_verifies_by_title_when_the_list_name_is_truncated():
    """列表名被游戏截断（「AK-12突…」）时，靠点开读标题确认。"""
    game = FakeGame([_skin("AK-12突击步枪-水墨", list_text="AK-12突…")])
    res = SkinFinder(game).find("AK-12突击步枪-水墨",
                                season="群星", tier="普通", gun="步枪")
    assert res["ok"] is True, res
    assert res["title"] == "AK-12突击步枪-水墨"


def test_does_not_click_unrelated_cards():
    """一屏全是无关的卡：一次都不点（省时间，也避免误选别的皮肤）。"""
    game = FakeGame([_skin("MK4冲锋枪-疾光魅影"), _skin("AKM突击步枪-群星"),
                     _skin("KC17突击步枪-造物纪元")])
    res = SkinFinder(game).find("QBZ95-1突击步枪-叠锋",
                                season="群星", tier="普通", gun="步枪")
    assert res["ok"] is False
    assert game.clicks == [], f"不该点任何卡：{game.clicks}"


def test_ocr_similarity_tolerates_one_wrong_character():
    """OCR 认错一个字（高手→高丢）也要能认对 —— 这是模糊匹配存在的理由。"""
    game = FakeGame([_skin("M250通用机枪-电玩高手S2", list_text="M250通用机枪-电玩高丢S2")])
    res = SkinFinder(game).find("M250通用机枪-电玩高手S2",
                                season="电玩高手S2", tier="传说", gun="轻机枪")
    assert res["ok"] is True, res
    assert res["score"] >= 0.9


def test_plans_go_from_precise_to_loose():
    """三档：精确组合 → 只按赛季 → 全库。

    精确组合只在「筛选表里这个组合确实有货」时才用 —— 表说 0 件还去筛，
    会把列表筛空、面板变成居中浮层（详见 core/skinfind.py）。
    """
    game = FakeGame([])
    plans = SkinFinder(game)._plans("X突击步枪-Y", "群星", "普通", "步枪")
    labels = [p["label"] for p in plans]
    assert labels[0] == "群星·普通·步枪", labels
    assert labels[-1] == "全库扫描", labels
    # 越往后条件越少
    def conds(p):
        return sum(1 for k in ("season", "tier", "gun") if p.get(k))
    counts = [conds(p) for p in plans]
    assert counts == sorted(counts, reverse=True), f"不是逐级放宽：{counts}"
