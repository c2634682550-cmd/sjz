"""这一版界面没有确认弹窗：点价格按钮直接成交。

真机实测（2026-10-22 在售页）：点一下价格按钮就买下一件，余额 40 → 38，
没有任何弹窗。原来的 `_fire_and_confirm` 会按 fire_timeout 连点等弹窗
（50Hz 下 8 秒 400 下）—— 没有弹窗拦着，就是买 400 件。

所以没有「确认」区域时改走 `_buy_without_dialog`：只点一次，用余额变化
判断成没成。这几个测试钉的就是这件事。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod


class FakeScreen:
    def __init__(self):
        self.frame = np.zeros((60, 400, 3), np.uint8)

    def grab(self, box=None):
        return self.frame

    def norm_to_px(self, box):
        return (0, 0, 400, 60)


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True

    def click(self, x, y, expect_foreground=None, **kw):
        self.clicks.append((x, y))


class FakeCfg:
    """真 Config 是对象：既按 key 取值，也有 region()/get()。"""

    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    def region(self, name):
        return (self.values.get("regions") or {}).get(name)


def _engine(monkeypatch, balances, record):
    e = engine_mod.Engine()
    e._target_hwnd = 123
    e._say = lambda msg, level="info": None
    e._record = record

    seq = list(balances)

    def fake_balance(_screen, _img=None):
        """余额走 `_balance` 的词框解析，这里直接给数字序列。

        原来 mock 的是 `ocr.read_text`；`_balance` 改成读词框（只认最右带 `+`
        的三角币组）之后就不走 read_text 了，mock 打空 → 余额永远 None →
        判定不出成交。这里改在 `_balance` 这一层给数，测的仍是「点一下余额
        掉 = 成交」。
        """
        if not seq:
            return None
        raw = seq.pop(0)
        tok = raw.split()[-1].rstrip("+") if raw.strip() else ""
        digits = "".join(ch for ch in tok if ch.isdigit())
        return int(digits) if digits else None

    monkeypatch.setattr(e, "_balance", fake_balance)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)
    # 「先识别再点」和结算弹窗各自有专门的测试；这里默认放行，免得它们
    # 抢走 fake_read 的返回序列。
    e._chip_ready = lambda screen: True
    e._bought_dialog = lambda screen: False
    return e


def test_one_click_and_a_lower_balance_means_bought(monkeypatch):
    records = []
    e = _engine(monkeypatch, ["2,049 0+ 0 40+", "2,049 0+ 0 38+"],
                lambda *a, **k: records.append(k))
    mouse = FakeMouse()

    ok = e._buy_without_dialog(FakeScreen(), mouse, (1600, 930), 0.0,
                               item="G3战斗步枪-叠锋", price=2)

    assert ok is True
    assert records and records[0]["result"] == "hit"


def test_unchanged_balance_is_recorded_as_miss(monkeypatch):
    records = []
    e = _engine(monkeypatch, ["2,049 0+ 0 40+"] + ["2,049 0+ 0 40+"] * 6,
                lambda *a, **k: records.append(k))
    mouse = FakeMouse()

    ok = e._buy_without_dialog(FakeScreen(), mouse, (1600, 930), 0.0,
                               item="G3战斗步枪-叠锋", price=2)

    assert ok is False
    assert len(mouse.clicks) > 1, "没成交就该一直连点到时间窗结束"
    # 两次落点不能完全一样（用户要求：两次不落同一个点）
    assert len({c for c in mouse.clicks}) >= 2, "两次点击落在同一个点上了"
    assert records and records[0]["result"] == "miss", "没成交却记成成交，等于骗自己"


def test_without_a_confirm_region_the_engine_never_enters_the_click_loop(monkeypatch):
    """没有确认区域时绝不能走「连点到超时」那条路。"""
    cfg = FakeCfg({"threshold": 0.85, "click_hz": 50, "fire_timeout_s": 8,
                   "confirm_clicks": 1, "confirm_interval_ms": 0,
                   "failsafe": False})
    records = []
    e = _engine(monkeypatch, ["2,049 0+ 0 40+", "2,049 0+ 0 38+"],
                lambda *a, **k: records.append(k))
    monkeypatch.setattr(engine_mod.Config, "load", staticmethod(lambda: cfg))
    mouse = FakeMouse()

    ok = e._fire_and_confirm(cfg, FakeScreen(), mouse, None, (1600, 930), 0.0)

    assert ok is True
    assert len(mouse.clicks) >= 1


def test_the_success_dialog_alone_means_bought(monkeypatch):
    """结算弹窗出来了就算成交 —— 这是唯一可靠的信号。

    弹窗开着时货币栏不渲染，余额永远读空（真机实测）；所以只认结算弹窗
    本身，余额掉了只是锦上添花。
    """
    records = []
    e = _engine(monkeypatch, [""] * 30, lambda *a, **k: records.append(k))
    e._balance = lambda scr, img=None: None
    e._bought_dialog = lambda screen: True
    mouse = FakeMouse()

    ok = e._buy_without_dialog(FakeScreen(), mouse, (1210, 682), 0.0,
                               item="R93-龙焱", price=2)

    assert ok is True
    assert records and records[0]["result"] == "hit"


def test_no_click_while_the_chip_still_says_unlock(monkeypatch):
    """价格行还写着「X秒后解锁购买」时一下都不许点 —— 先识别再点。"""
    e = engine_mod.Engine()
    e._target_hwnd = 123
    e._say = lambda msg, level="info": None

    class Scr:
        def norm_to_px(self, r):
            return (0, 0, 100, 40)

        def grab(self, box=None):
            return np.zeros((40, 100, 3), dtype="uint8")

    # 价格行上认出来的是解锁倒计时
    monkeypatch.setattr(engine_mod.ocr, "read_text", lambda img, **kw: "0分5秒后解锁购买")
    assert e._chip_ready(Scr()) is False

    # 订单还没开放购买，同样不能点
    monkeypatch.setattr(engine_mod.ocr, "read_text", lambda img, **kw: "订单尚未开放购买")
    assert e._chip_ready(Scr()) is False

    # 倒计时归零，剩下的是「剩余：X天」和价格
    monkeypatch.setattr(engine_mod.ocr, "read_text", lambda img, **kw: "剩余：2天23小时")
    assert e._chip_ready(Scr()) is True


def test_unreadable_chip_is_not_clicked(monkeypatch):
    """价格行上一个字都认不出来时也不点：宁可错过，不可乱点。"""
    e = engine_mod.Engine()

    class Scr:
        def norm_to_px(self, r):
            return (0, 0, 100, 40)

        def grab(self, box=None):
            return np.zeros((40, 100, 3), dtype="uint8")

    monkeypatch.setattr(engine_mod.ocr, "read_text", lambda img, **kw: "")
    assert e._chip_ready(Scr()) is False


def test_the_click_loop_really_asks_before_it_clicks(monkeypatch):
    """价格条没变成可买的样子时，循环里一下都不许点。

    这条钉的是循环本身：把 _chip_ready 的调用删掉、改成无脑连点，这里就红。
    """
    records = []
    e = _engine(monkeypatch, ["2,049 0+ 0 40+"] * 8,
                lambda *a, **k: records.append(k))
    e._chip_ready = lambda screen: False
    mouse = FakeMouse()

    ok = e._buy_without_dialog(FakeScreen(), mouse, (1600, 930), 0.0,
                               item="G3战斗步枪-叠锋", price=2)

    assert ok is False
    assert mouse.clicks == [], "价格条还不能点的时候点了，等于瞎点"


def test_no_confirm_region_delegates_to_single_click_buy_exactly_once(monkeypatch):
    """没有「确认」区域时必须走且只走一次 `_buy_without_dialog`。

    这条钉的是分支本身：以前 `confirm_region is None` 时继续往下走
    `expand(None)` 会 TypeError，或者退化成盲点循环。用 spy 记录调用，
    同时把模板匹配换成「谁调谁记」，确认它压根没进弹窗循环。
    """
    cfg = FakeCfg({"threshold": 0.85, "click_hz": 50, "fire_timeout_s": 0.1,
                   "confirm_clicks": 1, "confirm_interval_ms": 0,
                   "failsafe": False})
    e = _engine(monkeypatch, [], lambda *a, **k: None)
    delegations = []

    def spy_buy(screen, mouse, buy_click, t_start, item="", price=None):
        delegations.append((buy_click, item, price))
        return True

    e._buy_without_dialog = spy_buy
    matched = []
    monkeypatch.setattr(engine_mod, "match",
                        lambda *a, **k: matched.append(1) or (0.0, None))
    monkeypatch.setattr(engine_mod.Config, "load", staticmethod(lambda: cfg))

    ok = e._fire_and_confirm(cfg, FakeScreen(), FakeMouse(), "CONFIRM_TEMPLATE",
                             (1600, 930), 0.0, item="G3战斗步枪-叠锋", price=2)

    assert ok is True
    assert delegations == [((1600, 930), "G3战斗步枪-叠锋", 2)], (
        f"没有确认框时应该恰好转交一次无框购买，实际 {delegations}"
    )
    assert matched == [], "没有确认框却进了确认弹窗的模板匹配循环"


def test_confirm_region_present_but_no_dialog_returns_false_without_fallback(monkeypatch):
    """有「确认」区域但弹窗一直不出现：按没抢到记，绝不许退回单点购买。

    没有确认框才走单点购买；有确认框时如果偷偷退回去，等于在「等弹窗」
    vs 「直接成交」两种界面语义之间乱猜 —— 真实结果是买错东西。
    """
    cfg = FakeCfg({
        "threshold": 0.85, "click_hz": 50, "fire_timeout_s": 0.08,
        "confirm_clicks": 1, "confirm_interval_ms": 0, "failsafe": False,
        "regions": {"confirm": {"x": 0.3, "y": 0.3, "w": 0.4, "h": 0.5}},
    })
    records = []
    e = _engine(monkeypatch, [], lambda *a, **k: records.append(k))
    monkeypatch.setattr(engine_mod, "match", lambda *a, **k: (0.0, None))
    monkeypatch.setattr(engine_mod.Config, "load", staticmethod(lambda: cfg))
    e._buy_without_dialog = lambda *a, **k: pytest.fail(
        "有确认框却没等到弹窗时退回了无框单点购买")

    ok = e._fire_and_confirm(cfg, FakeScreen(), FakeMouse(), "CONFIRM_TEMPLATE",
                             (1600, 930), 0.0, item="G3战斗步枪-叠锋", price=2)

    assert ok is False
    assert records and records[0]["result"] == "miss", "没等到弹窗却记成了成交"


def test_insufficient_popup_aborts_the_fire_loop_after_one_buy_click(monkeypatch):
    """余额不足弹窗一出现：只点一次购买，点「取消」，抛 Aborted 收场。

    钱不够时游戏不弹确认框，直接弹「三角币不足」，右边就是「充值」。
    多点在开火循环里继续点购买，等于对着模态弹窗乱点。
    """
    cfg = FakeCfg({
        "threshold": 0.85, "click_hz": 50, "fire_timeout_s": 8.0,
        "confirm_clicks": 1, "confirm_interval_ms": 0, "failsafe": False,
        "regions": {"confirm": {"x": 0.3, "y": 0.3, "w": 0.4, "h": 0.5}},
    })
    e = _engine(monkeypatch, [], lambda *a, **k: None)
    # 真机语义：**点购买之前**没有弹窗；点完第一下才弹「三角币不足」。
    # 所以 `_insufficient_showing` 必须是有状态的 —— 开火前守门（
    # `_pre_fire_popup_guard`）要看到 False，点完之后才看到 True。
    state = {"popup": False}
    popup_result = {
        "found": True, "kind": "insufficient", "shortfall": 2001,
        "recommend": 328, "text": "您当前的三角币不足，是否前往充值？",
        "buttons": {"cancel": {"cx": 700, "cy": 600},
                    "recharge": {"cx": 900, "cy": 600}},
    }

    def fake_scan(_img):
        return dict(popup_result) if state["popup"] else {"found": False}

    def fake_cancel_point(res):
        btn = (res.get("buttons") or {}).get("cancel")
        return (btn["cx"], btn["cy"]) if btn else None

    class Clicking(FakeMouse):
        def click(self, x, y, expect_foreground=None, **kw):
            super().click(x, y, expect_foreground, **kw)
            if (x, y) == (1600, 930):
                state["popup"] = True          # 点购买 → 弹余额不足
            if (x, y) == (700, 600):
                state["popup"] = False         # 点取消 → 弹窗真的消失

    e._insufficient_showing = lambda screen: state["popup"]
    monkeypatch.setattr(engine_mod.popup, "scan", fake_scan)
    monkeypatch.setattr(engine_mod.popup, "cancel_point", fake_cancel_point)
    monkeypatch.setattr(engine_mod.Config, "load", staticmethod(lambda: cfg))

    mouse = Clicking()
    with pytest.raises(engine_mod.Aborted) as ei:
        e._fire_and_confirm(cfg, FakeScreen(), mouse, "CONFIRM_TEMPLATE",
                            (1600, 930), 0.0, item="G3战斗步枪-叠锋", price=2)

    assert "余额不足" in str(ei.value)
    assert mouse.clicks.count((1600, 930)) == 1, (
        f"余额不足弹窗出来后还在继续点购买：{mouse.clicks}"
    )
    assert (700, 600) in mouse.clicks, "认出了弹窗却没点「取消」，模态框会盖住后续操作"
    assert (900, 600) not in mouse.clicks, "点到了「充值」——这是真花钱的按钮"
