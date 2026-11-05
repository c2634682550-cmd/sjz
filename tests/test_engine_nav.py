"""引擎与导航的接缝。

钉住一条：列表模式必须先确认「已在在售页」，才能开始盯列表。
不确认的后果是引擎对着错误的页面 OCR、按错误的坐标点 —— 买错东西。
"""
import numpy as np
import pytest

from core import engine as eng_mod
from core.engine import Aborted, Engine


class FakeScreen:
    size = (1920, 1080)

    def grab(self, box=None):
        return np.zeros((1080, 1920, 3), np.uint8)

    def close(self):
        pass


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def click(self, x, y, verify=True, expect_foreground=None):
        self.clicks.append((x, y))


def _cfg(**task):
    return {"active_task": task}


def test_navigate_raises_aborted_when_navigation_fails(monkeypatch):
    """导航失败要抛 Aborted（不是静默继续）。

    Aborted 会被 _run 捕获并记成 ABORT 状态，用户能看见；如果静默继续，
    引擎会对着错误的列表开扫。
    """
    e = Engine()
    e._target_hwnd = 12345

    def boom(self, *a, **kw):
        raise eng_mod.NavError("测试：到不了在售页")

    monkeypatch.setattr(eng_mod.Navigator, "go_to_onsale", boom)
    with pytest.raises(Aborted, match="界面导航失败"):
        e._navigate(FakeScreen(), FakeMouse(), _cfg())


def test_navigate_passes_through_on_success(monkeypatch):
    """导航成功就正常返回，并记录到达的页面。"""
    e = Engine()
    e._target_hwnd = 12345
    logs = []
    e._say = lambda msg, level="info": logs.append((level, msg))

    monkeypatch.setattr(eng_mod.Navigator, "go_to_onsale",
                        lambda self, **kw: "detail")
    e._navigate(FakeScreen(), FakeMouse(), _cfg())
    assert any("已到在售页" in m for _, m in logs)


def test_navigate_uses_keyboard_for_f4(monkeypatch):
    """导航必须给 Navigator 传 Keyboard，否则按不了 F4。"""
    e = Engine()
    e._target_hwnd = 12345
    seen = {}

    def spy(self, **kw):
        seen["keyboard"] = self.keyboard
        seen["hwnd"] = getattr(self.keyboard, "hwnd", None)
        return "detail"

    monkeypatch.setattr(eng_mod.Navigator, "go_to_onsale", spy)
    e._navigate(FakeScreen(), FakeMouse(), _cfg())
    assert seen["keyboard"] is not None
    assert seen["hwnd"] == 12345


# ------------------------------------------------ 价格筛选接进导航（2026-10-21）

class FakeNav:
    """记录导航器上的调用顺序。

    核心是「顺序」：必须先开面板 → 设价格 → 点确定。顺序错了
    （比如先确定再设价格）游戏会忽略输入，筛选不生效。
    """

    def __init__(self):
        self.calls = []
        self.price_args = None
        self.price_returns: tuple | None = None   # 想让「读回」不等于设定值时填这里
        self.checks_args = []

    def ons_filter(self):
        self.calls.append("filter")

    def ons_price(self, lo=None, hi=None):
        self.calls.append("price")
        self.price_args = (lo, hi)
        if self.price_returns is not None:
            return self.price_returns
        return (lo, hi)

    def ons_checks(self, names, want=True):
        self.calls.append("checks")
        self.checks_args = list(names)
        return list(names)

    def ons_filter_confirm(self):
        self.calls.append("confirm")


def test_navigate_types_the_price_range_into_the_game():
    """价格区间要填进游戏的价格框 —— 这是「不买错档位」的第一道筛。

    顺序：开面板 → 设价格 → 确定。先确定再设价格游戏会忽略输入。
    """
    e = Engine()
    e._say = lambda msg, level="info": None

    nav = FakeNav()
    e._apply_price_filter(nav, _cfg(min_price=20000, max_price=50000))
    assert nav.calls == ["filter", "price", "confirm"], nav.calls
    assert nav.price_args == (20000, 50000), nav.price_args


def test_navigate_skips_price_filter_when_task_has_no_range():
    """任务没配价格 → 一个筛选动作都不该做。

    没配还去开面板设价格，会平白多几次点击，而且「重置」会把用户
    手动设的区间清掉。
    """
    e = Engine()
    e._say = lambda msg, level="info": None
    nav = FakeNav()
    e._apply_price_filter(nav, _cfg())
    assert nav.calls == [], f"不该有任何筛选动作，实际 {nav.calls}"


def test_navigate_sets_only_the_configured_price_side():
    """只配下限时上限传 None —— 不能顺手把上限改成 0（0 等于只买 0 金币）。"""
    e = Engine()
    e._say = lambda msg, level="info": None
    nav = FakeNav()
    e._apply_price_filter(nav, _cfg(min_price=8000))
    assert nav.calls == ["filter", "price", "confirm"], nav.calls
    assert nav.price_args == (8000, None), nav.price_args


def test_navigate_aborts_when_the_price_box_comes_back_empty_ranged():
    """读回 (1000, 2) 这种空区间 → 中止。

    真机踩过：要 3000，按键丢位数，框里只剩 2。上限 2 什么货都筛不出来，
    程序会一直空等 —— 比不抢更糟。
    """
    from core.engine import Aborted

    e = Engine()
    said = []
    e._say = lambda msg, level="info": said.append(msg)
    nav = FakeNav()
    nav.price_returns = (1000, 2)
    with pytest.raises(Aborted):
        e._apply_price_filter(nav, _cfg(min_price=1000, max_price=3000))
    assert any("价格" in m for m in said), said


def test_navigate_accepts_a_lower_bound_the_game_raised():
    """游戏把下限抬高（要 1000 变成 2050）算成功 —— 筛得更窄，是安全的。

    真机实测（2026-10-21）：游戏价格框不接受低于市场最低价的数，失焦后
    自己改回 2050。这不是失败，只是筛得更紧。
    """
    e = Engine()
    said = []
    e._say = lambda msg, level="info": said.append(msg)
    nav = FakeNav()
    nav.price_returns = (2050, 3000)
    e._apply_price_filter(nav, _cfg(min_price=1000, max_price=3000))
    assert nav.calls == ["filter", "price", "confirm"], nav.calls
    assert any("已生效" in m for m in said), said


def test_navigate_warns_when_the_filter_is_looser_than_asked():
    """下限被放宽（要 1000 框里 500）→ 告警但继续，不中止。

    为什么不再中止：价格框是深色底 + 细字，OCR 经常整个读空或读串，
    一中止整单就废了。而 `market.check` 后面会按 min/max 逐行再过滤一遍，
    区间外的货根本进不了购买那一步 —— 游戏内没筛窄不等于会买错。
    """
    e = Engine()
    said = []
    e._say = lambda msg, level="info": said.append((msg, level))
    nav = FakeNav()
    nav.price_returns = (500, 3000)
    e._apply_price_filter(nav, _cfg(min_price=1000, max_price=3000))
    assert nav.calls == ["filter", "price", "confirm"], nav.calls
    warns = [m for m, lvl in said if lvl == "warn"]
    assert any("最低价被放宽" in m for m in warns), said


def test_navigate_warns_when_the_upper_bound_is_looser_than_asked():
    """上限被放宽（要 3000 框里 99999）→ 告警但继续，不中止。"""
    e = Engine()
    said = []
    e._say = lambda msg, level="info": said.append((msg, level))
    nav = FakeNav()
    nav.price_returns = (2050, 99999)
    e._apply_price_filter(nav, _cfg(min_price=1000, max_price=3000))
    assert nav.calls == ["filter", "price", "confirm"], nav.calls
    warns = [m for m, lvl in said if lvl == "warn"]
    assert any("最高价被放宽" in m for m in warns), said


def test_navigate_aborts_when_the_price_range_is_empty():
    """区间是空的（框里 3000~1000）→ 中止。

    这条底线跟上面两条不一样：放宽只是「没筛窄」，后面逐行比对能兜住；
    空区间是「输入根本没进去」，游戏内筛出来的东西全是废的，没法兜。
    """
    e = Engine()
    e._say = lambda msg, level="info": None
    nav = FakeNav()
    nav.price_returns = (3000, 1000)
    with pytest.raises(Aborted):
        e._apply_price_filter(nav, _cfg(min_price=1000, max_price=3000))


def test_navigate_applies_material_and_color_filter():
    """任务配了材质/颜色时，要在同一次开面板里勾上，再点确定。

    顺序必须是 filter → checks(材质) → checks(颜色) → confirm。
    每次开关面板要 1.5s+，所以不能一项开一次。
    """
    from core.engine import Engine
    nav = FakeNav()
    e = Engine.__new__(Engine)
    said = []
    e._say = lambda m, lvl="info": said.append(m)
    e._apply_price_filter(nav, _cfg(materials=["速度线"], colors=["黑色"]))

    assert nav.calls == ["filter", "checks", "checks", "confirm"], nav.calls
    assert "price" not in nav.calls, "没配价格区间就不该设价格"


def test_navigate_skips_filter_when_nothing_configured():
    """什么都没配就不开面板 —— 开面板再关会打断盯列表。"""
    from core.engine import Engine
    nav = FakeNav()
    e = Engine.__new__(Engine)
    e._say = lambda m, lvl="info": None
    e._apply_price_filter(nav, _cfg())
    assert nav.calls == [], f"不该有任何调用，实际 {nav.calls}"


def test_navigate_price_plus_material_together():
    """价格 + 材质同时配：价格不填，材质照勾 —— filter → checks → confirm。"""
    from core.engine import Engine
    nav = FakeNav()
    e = Engine.__new__(Engine)
    e._say = lambda m, lvl="info": None
    e._apply_price_filter(nav, _cfg(min_price=20000, max_price=50000,
                                    materials=["贵金属"]))
    assert nav.calls == ["filter", "price", "checks", "confirm"], nav.calls
    assert nav.price_args == (20000, 50000), nav.price_args
