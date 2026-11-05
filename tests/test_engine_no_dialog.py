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

    def click(self, x, y, expect_foreground=None):
        self.clicks.append((x, y))


class FakeCfg:
    """真 Config 是对象：既按 key 取值，也有 region()。"""

    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        return self.values[key]

    def region(self, name):
        return None


def _engine(monkeypatch, balances, record):
    e = engine_mod.Engine()
    e._target_hwnd = 123
    e._say = lambda msg, level="info": None
    e._record = record

    seq = list(balances)

    def fake_read(_img, **kw):
        return seq.pop(0) if seq else ""

    monkeypatch.setattr(engine_mod.ocr, "read_text", fake_read)
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
