"""品阶枚举的离线用例（不碰真机）。

锁死三件事：
  1. 品阶字段由筛选上下文继承 —— 恒为 `普通（上下文继承）`
  2. 面板回读不符（勾选框 / 赛季下拉）→ 整个枚举作废并抛 FilterContextError
  3. 两次独立枚举条数必须相同；不同就给出差异条目清单，不取其一
"""
import pytest

from core import tier_enum


class FakeNav:
    """按 pass 供给列表屏；勾选框是否真的跟随点击由 honor 控制。"""

    def __init__(self, passes=None, tier=(False, False, False, True),
                 season_text="美杜莎", honor=True, season_match=True,
                 honor_pick=True):
        self.passes = passes or [[[]]]
        self.tier = list(tier)
        self.season_text = season_text
        self.current = season_text
        self.honor = honor
        self.honor_pick = honor_pick
        self.season_match = season_match
        self._p = -1
        self._i = 0
        self.calls = []
        self.scanned = False
        self.confirmed = 0

    # ---- 面板读取 ----
    def _snap(self):
        return object()

    def _read_boxes(self, _img):
        return {"tier": list(self.tier), "owned": True, "not_owned": True}

    def _season_now(self):
        return self.current

    def _season_match(self, target, text):
        return self.season_match

    # ---- 面板动作 ----
    def col_filter(self):
        self.calls.append("col_filter")

    def col_pick_season(self, name):
        self.calls.append(("season", name))
        if self.honor_pick:
            self.current = name

    def col_apply(self, tier=None, guns=None, owned=None, not_owned=None):
        self.calls.append(("apply", list(tier) if tier is not None else None))
        if self.honor and tier is not None:
            self.tier = list(tier)

    def col_filter_confirm(self):
        self.confirmed += 1

    def _panel_open(self):
        return False

    # ---- 列表 ----
    def skin_scroll_top(self):
        self.scanned = True
        self._p += 1
        self._i = 0

    def skin_scroll(self, _notches):
        pass

    def skin_items(self):
        if self._p < 0 or self._p >= len(self.passes):
            return []
        screens = self.passes[self._p]
        if self._i >= len(screens):
            return []
        scr = screens[self._i]
        self._i += 1
        return [(t, 100) for t in scr]


def test_grade_columns_order():
    assert tier_enum.GRADE_COLUMNS == ("传说", "史诗", "稀有", "普通")
    assert tier_enum.grade_field("普通") == "普通（上下文继承）"


def test_items_inherit_grade_from_filter_context():
    nav = FakeNav(passes=[[["AK-12突击步枪", "M4A1突击步枪"]]])
    row = tier_enum.enumerate_season(nav, "美杜莎", grade="普通", passes=1)
    assert row["count"] == 2
    assert all(it["grade"] == "普通（上下文继承）" for it in row["items"])
    assert all(it["grade_source"] == "filter_context" for it in row["items"])


def test_context_snapshot_is_recorded():
    nav = FakeNav(passes=[[["AK-12突击步枪"]]])
    row = tier_enum.enumerate_season(nav, "美杜莎", grade="普通", passes=1)
    ctx = row["context"]
    assert ctx["season"] == "美杜莎"
    assert ctx["grade"] == "普通"
    assert ctx["source"] == "panel_readback"
    assert ctx["season_readback"] == "美杜莎"
    assert ctx["tier_readback"] == [False, False, False, True]


def test_apply_filter_ticks_only_the_wanted_grade():
    nav = FakeNav(passes=[[["AK-12突击步枪"]]])
    tier_enum.enumerate_season(nav, "美杜莎", grade="普通", passes=1)
    applied = [c[1] for c in nav.calls if isinstance(c, tuple) and c[0] == "apply"]
    assert applied == [[False, False, False, True]]


def test_tier_readback_mismatch_aborts_whole_enumeration():
    # 勾选框不跟随点击（面板状态没变）→ 回读不符 → 作废，不许继续扫
    nav = FakeNav(passes=[[["AK-12突击步枪"]]], tier=(True, True, True, True),
                  honor=False)
    with pytest.raises(tier_enum.FilterContextError, match="品阶回读不符"):
        tier_enum.enumerate_season(nav, "美杜莎", grade="普通", passes=1)
    assert nav.scanned is False
    assert nav.confirmed == 0


def test_season_readback_mismatch_aborts_whole_enumeration():
    nav = FakeNav(passes=[[["AK-12突击步枪"]]], season_text="群星",
                  season_match=False, honor_pick=False)
    with pytest.raises(tier_enum.FilterContextError, match="赛季回读不符"):
        tier_enum.enumerate_season(nav, "美杜莎", grade="普通", passes=1)
    assert nav.scanned is False


def test_prefix_collision_does_not_pass_readback():
    # 棱镜攻势 vs 棱镜攻势S2：前缀相同，半字匹配会误判通过
    assert tier_enum.season_readback_ok("棱镜攻势", "棱镜攻势S2") is False
    assert tier_enum.season_readback_ok("棱镜攻势S2", "棱镜攻势S2") is True


def test_prefix_collision_is_fixed_by_neutral_reset():
    # 真机 15:24 的事故：下拉框停在 S2，目标 S1，被「已选中」跳过
    nav = FakeNav(passes=[[["A"]]], season_text="棱镜攻势S2")
    row = tier_enum.enumerate_season(nav, "棱镜攻势", passes=1)
    picked = [c[1] for c in nav.calls if isinstance(c, tuple) and c[0] == "season"]
    assert picked == ["全部赛季", "棱镜攻势"]
    assert row["context"]["season_readback"] == "棱镜攻势"


def test_two_passes_consistent():
    screens = [["A", "B", "C"], ["B", "C", "D"], ["C", "D", "E"]]
    nav = FakeNav(passes=[screens, screens])
    row = tier_enum.enumerate_season(nav, "美杜莎", grade="普通", passes=2)
    assert row["counts"] == [5, 5]
    assert row["consistent"] is True
    assert row["count"] == 5
    assert row["diff"] == []


def test_two_passes_mismatch_reports_diff_and_refuses_to_pick_one():
    first = [["A", "B", "C"]]
    second = [["A", "B", "C"], ["D"]]
    nav = FakeNav(passes=[first, second])
    row = tier_enum.enumerate_season(nav, "美杜莎", grade="普通", passes=2)
    assert row["consistent"] is False
    assert row["count"] is None                     # 不许取其一
    assert ("D", 0, 1) in row["diff"]


def test_scan_list_dedupes_overlap_and_stops_at_bottom():
    nav = FakeNav(passes=[[["A", "B", "C"], ["B", "C", "D"], ["C", "D", "E"]]])
    got = tier_enum.scan_list(nav)
    assert got["names"] == ["A", "B", "C", "D", "E"]


def test_empty_list_counts_zero():
    nav = FakeNav(passes=[[[], []]])
    row = tier_enum.enumerate_season(nav, "美杜莎", grade="普通", passes=1)
    assert row["count"] == 0
    assert row["items"] == []


def test_confirm_retries_until_panel_closes():
    class Flaky(FakeNav):
        def __init__(self):
            super().__init__(passes=[[["A"]]])
            self.n = 0
            self.open = True

        def col_filter_confirm(self):
            self.n += 1
            if self.n >= 3:
                self.open = False

        def _panel_open(self):
            return self.open

    nav = Flaky()
    assert tier_enum._confirm_panel(nav) is True
    assert nav.n >= 3


def test_confirm_gives_up_and_aborts():
    class Stuck(FakeNav):
        def __init__(self):
            super().__init__(passes=[[["A"]]])
            self.closed = False

        def _panel_open(self):
            return True

        def close_filter_panel(self):
            self.closed = True

    nav = Stuck()
    with pytest.raises(tier_enum.FilterContextError, match="关上面板"):
        tier_enum._confirm_panel(nav)
    assert nav.closed is True


def test_enumerate_seasons_totals():
    # 两个赛季 × 两遍，每遍一屏
    nav = FakeNav(passes=[[["A", "B"]], [["A", "B"]],
                          [["C", "D", "E"]], [["C", "D", "E"]]])
    out = tier_enum.enumerate_seasons(nav, ["美杜莎", "群星"], passes=2)
    assert out["total"] == 5
    assert out["consistent"] is True
    assert [r["season"] for r in out["seasons"]] == ["美杜莎", "群星"]
