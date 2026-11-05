"""列表模式：按屏幕顺序一件件抢，全试完就刷新从第一件重来。

真机需求（2026-10-21，用户原话）：
  「抢页面第一个，如果没抢到按顺序抢第二个，以此类推直到抢到皮肤；
    如果这一页的皮肤你都没抢到你就点一下刷新，接着抢第一个，以此循环」

所以列表模式不是「挑最便宜的一件」，而是：
  1. 只看「不显示公示期」—— 公示期到点也抢不到，别浪费时间；
  2. 严格按屏幕顺序（先上后下、同一排先左后右）；
  3. 一件失败就试下一件；
  4. 一页试完 → 刷新 → 从第一件重新来。
"""
import threading

import numpy as np
import pytest

from core import engine as eng_mod
from core import market
from core.engine import DONE, Engine


def _row(x0, y0, price=2050.0, text="KC17 突击步枪造物纪元 极品 成色 A"):
    return {"text": text, "price": float(price), "craft": None, "wear": None,
            "wear_value": None, "x0": float(x0), "y0": float(y0),
            "cy": float(y0) + 20.0}


LEFT = _row(40.0, 10.0)
RIGHT = _row(700.0, 10.0)          # 同一排右边那张
LOWER = _row(40.0, 220.0)


def test_pick_follows_screen_order_not_price():
    """同一排两张卡 y0 相同，必须靠 x0 分出先后，否则顺序随 OCR 返回顺序乱跳。"""
    task = {"min_price": 1000, "max_price": 3000, "sort": "order"}
    rows = [LOWER, RIGHT, LEFT]
    assert market.pick(rows, task)[0] is LEFT
    rows2 = [RIGHT, LEFT]
    assert market.pick(rows2, task)[0] is LEFT, "同一排先左后右"


def test_pick_ignores_price_when_sorting_by_order():
    """顺序模式下贵的在前面也先抢它 —— 用户要的是「页面第一个」。"""
    cheap = _row(40.0, 400.0, price=1200.0)
    dear = _row(40.0, 10.0, price=2900.0)
    task = {"min_price": 1000, "max_price": 3000, "sort": "order"}
    assert market.pick([cheap, dear], task)[0] is dear


class FakeCfg(dict):
    def __init__(self, **kw):
        super().__init__({
            "threshold": 0.8, "failsafe": False, "click_hz": 50,
            "fire_timeout_s": 0.2, "confirm_clicks": 1,
            "confirm_interval_ms": 0, "list_scan_interval": 0.0,
            # 兜底超时：逻辑写坏时测试要「红」，不能挂死
            "list_scan_timeout": 5.0, "list_detail_timeout": 0.2,
            "list_max_tries": 50,
            "regions": {"list": {"x": 0, "y": 0, "w": 1, "h": 1},
                        "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1},
                        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.1}},
            "active_task": {"min_price": 1000, "max_price": 3000,
                            "sort": "order"},
        })
        self.update(kw)

    def region(self, name):
        return self["regions"].get(name)

    def has_template(self, name):
        return True


class FakeScreen:
    """列表画面固定，grab 永远给同一张图（签名稳定 → 只 OCR 一次）。"""

    size = (1920, 1080)

    def norm_to_px(self, box):
        return (60, 210, 1340, 730)

    def grab(self, px=None):
        return np.zeros((8, 8, 3), np.uint8)

    def close(self):
        pass


class FakeNav:
    made = []
    page: tuple[str, float] = ("onsale", 0.9)   # 当前页面，测试里可改

    def __init__(self, *a, **k):
        FakeNav.made.append(self)
        self.calls = []

    can_switch = True          # to_onsale() 能不能切回在售页

    def where(self):
        return FakeNav.page

    def to_onsale(self):
        FakeNav.made[-1].calls.append(("to_onsale",))
        if FakeNav.can_switch:
            FakeNav.page = ("onsale", 0.9)
        return FakeNav.can_switch

    def pub_filter(self, want):
        self.calls.append(("pub_filter", want))

    def ons_refresh(self):
        self.calls.append(("refresh",))

    # 价格筛选（_ons_ready → _apply_price_filter 会走这三步）
    def ons_filter(self):
        self.calls.append(("filter",))

    price_returns = None

    def ons_price(self, lo=None, hi=None):
        self.calls.append(("price", lo, hi))
        if FakeNav.price_returns is not None:
            return FakeNav.price_returns
        return (lo, hi)

    def ons_checks(self, names, want=True):
        self.calls.append(("checks", tuple(names)))
        return list(names)

    def ons_filter_confirm(self):
        self.calls.append(("confirm",))


class FakeMouse:
    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True


def _run(monkeypatch, rows, fire_results):
    """跑一次 _execute_list。fire_results 是 _fire_and_confirm 的返回值序列。"""
    e = Engine()
    e._stop = threading.Event()
    e._target_hwnd = 1
    e._said = []
    e._say = lambda msg, level="info": e._said.append((level, msg))
    e._failsafe = lambda on: None

    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(eng_mod.market, "scan", lambda frame: list(rows))
    monkeypatch.setattr(eng_mod, "match", lambda a, b: (1.0, None))
    monkeypatch.setattr(e, "_load", staticmethod(lambda cfg, name: np.zeros((4, 4))))
    clicks = []
    monkeypatch.setattr(e, "_click", lambda m, x, y: clicks.append((x, y)))

    seq = list(fire_results)

    def fire(*a, **k):
        return seq.pop(0) if seq else False

    monkeypatch.setattr(e, "_fire_and_confirm", fire)
    return e, clicks


def test_tries_cards_in_screen_order_until_one_succeeds(monkeypatch):
    e, clicks = _run(monkeypatch, [LEFT, RIGHT, LOWER], [False, True])
    e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)

    assert e._state == DONE
    assert len(clicks) == 2, "第一件失败后要换第二件"
    assert clicks[0] != clicks[1]
    assert all(isinstance(v, int) for xy in clicks for v in xy), \
        "SetCursorPos 只收整数"
    # 先点左边那张（同一排的第一张），再点右边那张
    assert clicks[0][0] < clicks[1][0]


def test_takes_the_first_card_even_when_it_is_not_the_cheapest(monkeypatch):
    """「抢页面第一个」—— 第一张比第二张贵，也先抢第一张。

    这条和捡漏的直觉相反，但用户要的就是顺序：越靠后越难等到。
    """
    dear = _row(40.0, 10.0, price=2900.0)      # 左上
    cheap = _row(700.0, 10.0, price=1200.0)    # 右上，更便宜
    e, clicks = _run(monkeypatch, [cheap, dear], [True])
    e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)
    assert len(clicks) == 1
    assert clicks[0][0] < 700, f"应该点左上那张，实际点了 {clicks[0]}"


def test_refreshes_and_restarts_when_the_page_is_exhausted(monkeypatch):
    """三件全试完 → 刷新 → 从第一件重来（用户要的正是这个循环）。"""
    e, clicks = _run(monkeypatch, [LEFT, RIGHT, LOWER], [False, False, False, True])
    e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)

    assert e._state == DONE
    nav = FakeNav.made[0]
    # 起手切筛选后刷过一次，所以一页试完时应当出现第 2 次刷新
    assert nav.calls.count(("refresh",)) >= 2, "一页试完必须刷新"
    assert len(clicks) == 4
    assert clicks[3] == clicks[0], "刷新后要重新从第一件抢起"


def test_list_mode_excludes_publicity_period(monkeypatch):
    """起手就把筛选切到「不显示公示期」——公示期到点也抢不到。"""
    e, _ = _run(monkeypatch, [LEFT], [True])
    e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)
    assert ("pub_filter", "不显示公示期") in FakeNav.made[0].calls


def test_idle_refresh_when_nothing_matches_for_a_while(monkeypatch):
    """一件都不符合条件时，过一会儿也要刷新 —— 新挂单随时会冒出来。

    光等是等不到的：不刷新就永远看着同一批卖不动的货。
    """
    monkeypatch.setattr(eng_mod, "QUEUE_IDLE_REFRESH_S", 0.01)
    e, clicks = _run(monkeypatch, [_row(40.0, 10.0, price=99999.0)], [])
    e._execute_list(FakeCfg(list_scan_timeout=0.12), FakeScreen(),
                    FakeMouse(), None)
    assert not clicks, "没有符合条件的货，不许点任何卡片"
    assert FakeNav.made[0].calls.count(("refresh",)) >= 2, "该周期性刷新"


def test_no_refresh_loop_when_nothing_matches(monkeypatch):
    """一件都不符合条件时不该疯狂刷新（那是空转，不是「这一页试完了」）。"""
    e, clicks = _run(monkeypatch, [_row(40.0, 10.0, price=99999.0)], [])
    # 没有候选就会一直等，靠扫描超时收尾（不是靠用户按停）
    e._execute_list(FakeCfg(list_scan_timeout=0.05), FakeScreen(),
                    FakeMouse(), None)
    assert not clicks
    # 起手那一次刷新是必须的（切完筛选要重拉列表），之后不该再刷
    assert FakeNav.made[0].calls.count(("refresh",)) == 1


def test_list_mode_sets_the_price_range_before_scanning(monkeypatch):
    """列表模式起手要：切「不显示公示期」→ 开面板设价格 → 确定 → 刷新。

    价格不设就扫列表，等于在全档位里按行筛 —— 用户要的是游戏里先筛好，
    列表里只剩 1000~3000 的货。
    """
    FakeNav.price_returns = None
    e, clicks = _run(monkeypatch, [LEFT], [True])
    e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)
    calls = FakeNav.made[0].calls
    assert calls[0] == ("pub_filter", "不显示公示期"), calls
    assert ("price", 1000, 3000) in calls, calls
    assert calls.index(("filter",)) < calls.index(("price", 1000, 3000)) \
        < calls.index(("confirm",)), f"顺序必须是 开面板→设价格→确定：{calls}"
    assert ("refresh",) in calls, "设完筛选要刷新"


def test_list_mode_refuses_to_scan_when_the_price_box_is_wrong(monkeypatch):
    """价格框没设进去 → 一件都不许点，直接停。

    真机症状：要 3000 读回 2。这时候列表是全档位的，按行挑会买到几万
    金币的货 —— 宁可不抢。
    """
    from core.engine import Aborted

    FakeNav.price_returns = (1000, 2)
    try:
        e, clicks = _run(monkeypatch, [LEFT, RIGHT], [True, True])
        with pytest.raises(Aborted):
            e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)
    finally:
        FakeNav.price_returns = None
    assert not clicks, f"筛不进去就不许点：{clicks}"


def test_list_mode_refuses_to_run_on_the_recommend_page(monkeypatch):
    """不在「在售」页就中止 —— 榜单页上点筛选会点错地方。

    真机踩过：推荐页顶部也有「在售/推荐」，判据只认「推荐」时榜单页被当成
    在售页，程序在榜单页上点 (435,197) 切公示期筛选，扫的全是榜单文字。
    """
    from core.engine import Aborted

    FakeNav.page = ("recommend", 0.9)
    FakeNav.can_switch = False
    try:
        e, clicks = _run(monkeypatch, [LEFT], [True])
        with pytest.raises(Aborted):
            e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)
    finally:
        FakeNav.page = ("onsale", 0.9)
        FakeNav.can_switch = True
    assert not clicks, "切不到在售页就一件都不许点"


def test_list_mode_switches_back_to_onsale_when_it_is_on_recommend(monkeypatch):
    """站错在「推荐」页时自己切回「在售」再开工 —— 而不是直接放弃。"""
    FakeNav.page = ("recommend", 0.9)
    try:
        e, clicks = _run(monkeypatch, [LEFT], [True])
        e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)
    finally:
        FakeNav.page = ("onsale", 0.9)
    assert ("to_onsale",) in FakeNav.made[0].calls, FakeNav.made[0].calls
    assert e._state == DONE

# ---------------- 本轮限量 / 预算上限 ----------------
# 参考「抢够 2 件即跳下一个目标」「预算 3200」：抢购不是抢到一件就收工，
# 也不是无限花 —— 这两条都是钱的事，写错就是多买或者买贵。

def test_quota_keeps_going_after_the_first_hit(monkeypatch):
    """限量 2 件：抢到第一件要回列表继续，不是当场收工。"""
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 2})
    e, clicks = _run(monkeypatch, [LEFT, RIGHT], [True, True])
    e._execute_list(cfg, FakeScreen(), FakeMouse(), None)

    assert e._state == DONE
    assert len(clicks) == 2, f"要抢两件，实际点了 {len(clicks)} 次"
    assert clicks[1] == clicks[0], "抢完一件回列表要从第一件重新开始"
    assert FakeNav.made[0].calls.count(("refresh",)) >= 2, "抢到一件后要刷新列表"


def test_quota_one_stops_after_a_single_hit(monkeypatch):
    """默认限量 1 件：抢到就收工，不许顺手再抢第二件。"""
    e, clicks = _run(monkeypatch, [LEFT, RIGHT], [True, True])
    e._execute_list(FakeCfg(), FakeScreen(), FakeMouse(), None)

    assert e._state == DONE
    assert len(clicks) == 1, f"只该抢一件，实际点了 {len(clicks)} 次"


def test_budget_skips_cards_it_cannot_afford(monkeypatch):
    """预算 3000：2000 的能买，5000 的买不起 —— 买不起的那张一次都不许点。"""
    cheap = _row(40.0, 10.0, price=2000.0)
    dear = _row(700.0, 10.0, price=5000.0)
    cfg = FakeCfg(active_task={"min_price": 0, "max_price": 0, "sort": "order",
                               "quota": 5, "budget": 3000})
    e, clicks = _run(monkeypatch, [cheap, dear], [True, True, True])
    e._execute_list(cfg, FakeScreen(), FakeMouse(), None)

    assert len(clicks) == 1, f"超预算的卡片不许点，实际点了 {clicks}"
    assert clicks[0][0] < 700, "该点便宜那张"
    assert any("超预算" in msg for _lvl, msg in e._said), e._said


def test_budget_counts_what_was_already_spent(monkeypatch):
    """预算 5000：第一件 3000 成交后只剩 2000，剩下 4000/3000 的都不许再点。"""
    first = _row(40.0, 10.0, price=3000.0)
    second = _row(40.0, 220.0, price=4000.0)
    cfg = FakeCfg(active_task={"min_price": 0, "max_price": 0, "sort": "order",
                               "quota": 5, "budget": 5000})
    e, clicks = _run(monkeypatch, [first, second], [True, True, True])
    e._execute_list(cfg, FakeScreen(), FakeMouse(), None)

    assert len(clicks) == 1, f"预算不够时不许点第二件，实际点了 {clicks}"

