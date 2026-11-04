# -*- coding: utf-8 -*-
"""C 组（输入注入 / 交易交互）第三轮审查问题的离线回归。

全部离线：不注入真实输入、不启动游戏/GUI、不联网。
对应 `_code_review_3round.md` 的 I1~I22（I23/I24 的文件不在本组范围内）。
"""
import ctypes
import os
import sys
import threading
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bricks, cards, hotkey, input as input_mod, market, popup  # noqa: E402
from core import skinfind  # noqa: E402


# ---------------------------------------------------------------- I2 价格解析
def test_squeeze_digits_does_not_glue_wear_to_price():
    """`0.120 9,500`（磨损 + 价格）不能被挤成 `0.1209,500`。"""
    got = market._squeeze_digits("M4A1突击步枪-纯白涂装 0.120 9,500")
    assert "0.120 9,500" in got
    assert "0.1209" not in got


def test_parse_row_reads_price_after_wear():
    row = market.parse_row("M4A1突击步枪·纯白涂装 0.120 9,500")
    assert row["price"] == 9500
    assert row["wear_value"] == pytest.approx(0.120)


def test_parse_row_does_not_treat_wear_decimal_as_price():
    """`0.1209` 是磨损（4 位小数），不是价格。"""
    assert market.parse_row("成色 S 0.1209")["price"] is None
    assert market.parse_row("成色 S 05")["price"] is None


def test_parse_row_still_squeezes_plain_thousands():
    """小数点保护不能把「逗号被认成空格」的千位挤没了。"""
    assert market.parse_row("15 囤 000")["price"] == 15000
    assert market.parse_row("015 252")["price"] == 15252


# ---------------------------------------------------------------- I1 价格只取价格带
@pytest.mark.parametrize("text,want", [
    ("成色 S 05", ""),          # 成色带里的残数字
    ("成色 S 0.1209", ""),      # 成色带里的磨损
    ("成色 S 14,000", "14,000"),
    ("极品 | 成色 B 9.000", "9.000"),
    ("QBZ951 成色 A 017000", "017000"),
    ("05", "05"),               # 价格单独成带：两个三角币的挂单是真的
    ("0.1209", ""),             # 单独成带的小数仍是磨损
])
def test_price_token_in_mixed_band(text, want):
    assert cards._price_token_in(text) == want


# ---------------------------------------------------------------- I3 未知价拦截
def test_check_rejects_unknown_price_by_default():
    row = market.parse_row("M4A1突击步枪·纯白涂装 成色 A")
    row["price"] = None
    ok, why = market.check(row, {})
    assert not ok and "价格" in why


def test_check_allows_unknown_price_only_when_configured():
    row = market.parse_row("M4A1突击步枪·纯白涂装 成色 A")
    row["price"] = None
    ok, _why = market.check(row, {"allow_unknown_price": True})
    assert ok


def test_check_still_passes_known_price_without_bounds():
    row = market.parse_row("M4A1突击步枪·纯白涂装 成色 A 1,000")
    ok, _why = market.check(row, {})
    assert ok


# ---------------------------------------------------------------- I12 排除键 2D
def _row(y, x, price=1000.0):
    row = market.parse_row(f"M4A1突击步枪·纯白涂装 成色 A {price:,.0f}")
    row.update({"cy": y, "y0": y - 40, "x0": x, "name": "M4A1突击步枪·纯白涂装"})
    return row


def test_pick_exclude_tuple_does_not_skip_the_whole_row():
    """同一横排两张卡 y 相同，按 (x0, cy) 排除只该排除被点过的那张。"""
    left, right = _row(300, 10, 1000), _row(300, 400, 2000)
    got, _why, _diag = market.pick([left, right], {}, exclude=[(10, 300)])
    assert got is right


def test_pick_exclude_still_accepts_a_bare_y():
    """老调用方只传 y，行为保持不变。"""
    row = _row(300, 10)
    got, _why, diag = market.pick([row], {}, exclude=[300])
    assert got is None and diag[0]["why"] == "已经点过"


# ---------------------------------------------------------------- I13 卡片数变化
def test_changed_cards_all_when_card_count_changes():
    frame = np.zeros((10, 10, 3), np.uint8)
    cards_now = [(0, 0, 10, 10)] * 3
    idx = cards.changed_cards(frame, frame.copy(), cards_now,
                              prev_cards=[(0, 0, 10, 10)] * 2)
    assert idx == [0, 1, 2]


def test_changed_cards_same_count_still_diffs():
    frame = np.zeros((10, 10, 3), np.uint8)
    cur = frame.copy()
    cur[0:10, 0:10] = 255
    cards_now = [(0, 0, 10, 10)]
    idx = cards.changed_cards(frame, cur, cards_now, prev_cards=cards_now)
    assert idx == [0]


# ---------------------------------------------------------------- I9 目标校验
def test_open_devices_fails_when_no_game_window(monkeypatch):
    """找不到游戏窗口就显式失败，不许拿前台窗口顶替。"""
    from core import actions, nav, screen as screen_mod

    class _FakeScreen:
        def __init__(self, monitor=1):
            self.bounds = {"left": 0, "top": 0, "width": 1920, "height": 1080}

        def close(self):
            pass

    monkeypatch.setattr(screen_mod, "Screen", _FakeScreen)
    monkeypatch.setattr(input_mod, "find_game_window", lambda: 0)
    with pytest.raises(nav.NavError, match="找不到游戏窗口"):
        actions._open_devices({})


# ---------------------------------------------------------------- I11 点击构造
def test_keyboard_click_builds_a_fully_initialised_mouse(monkeypatch):
    """Keyboard.click 不能再走 Mouse.__new__：构造出来的对象字段必须齐全。"""
    seen = {}

    def fake_click(self, x, y, **kw):
        seen["x"], seen["y"] = x, y
        seen["mode"] = self._mode
        seen["vx"] = self._vx
        seen["warn"] = self._drv_warn

    monkeypatch.setattr(input_mod.Mouse, "click", fake_click)
    kb = input_mod.Keyboard(1234, mode="legacy")
    kb.click(10, 20)
    assert seen["x"] == 10 and seen["y"] == 20
    assert seen["mode"] == "legacy"
    assert isinstance(seen["vx"], int)


def test_mouse_accepts_a_none_screen():
    m = input_mod.Mouse(None, mode="legacy")
    assert m._origin_left == 0 and m._origin_top == 0
    assert m._vx == input_mod._user32.GetSystemMetrics(input_mod._SM_XVIRTUALSCREEN)


# ---------------------------------------------------------------- I21 虚拟键码
@pytest.mark.parametrize("key,vk", [("A", 0x41), ("Z", 0x5A), ("0", 0x30), ("9", 0x39)])
def test_vk_table_covers_letters_and_digits(key, vk):
    assert input_mod.VK[key] == vk


# ---------------------------------------------------------------- I22 残破行
def test_shortfall_reads_past_a_broken_row():
    rows = [{"text": "缺少〔 ( 三角币"}, {"text": "缺少 2,001 三角币"}]
    assert popup._shortfall(rows) == 2001


def test_shortfall_returns_none_when_all_rows_are_broken():
    assert popup._shortfall([{"text": "缺少〔)"}, {"text": "没有金币"}]) is None


# ---------------------------------------------------------------- I16 结构尺寸
def test_mouse_stroke_is_twenty_bytes():
    from core import interception

    assert ctypes.sizeof(interception.MouseStroke) == 20
    assert ctypes.sizeof(interception.KeyStroke) == 8


# ---------------------------------------------------------------- I4 只给型号
def test_find_skin_refuses_a_bare_model_name():
    rows = [r for r in bricks.catalog() if r.get("model") and r.get("name")]
    assert rows, "知识库为空，测试前提不成立"
    row = rows[0]
    assert bricks.find_skin(row["model"]) is None, "只给型号不该猜皮肤"
    assert bricks.find_skin(row["name"]) is not None


def test_skinfilter_plan_is_none_for_a_bare_model():
    from core import skinfilter

    rows = [r for r in bricks.catalog() if r.get("model")]
    assert rows
    assert skinfilter.plan(rows[0]["model"]) is None


# ---------------------------------------------------------------- I5 开卡校验
class _StubNav:
    def __init__(self, titles):
        self._titles = list(titles)
        self.clicks = 0

    def skin_title(self):
        return self._titles.pop(0) if len(self._titles) > 1 else self._titles[0]

    def click(self, *a, **kw):
        self.clicks += 1


def test_open_and_verify_waits_for_the_title_to_actually_change():
    """点开瞬间读到的是上一张卡的标题，不能拿它判定「不是它」。"""
    nav = _StubNav(["旧标题", "旧标题", "AS Val突击步枪·夜穹"])
    finder = types.SimpleNamespace(nav=nav, _say=lambda *a, **kw: None)
    got = skinfind.SkinFinder._open_and_verify(finder, "AS Val突击步枪·夜穹",
                                               300, "AS Val突击步枪·夜穹")
    assert got is not None and got["title"] == "AS Val突击步枪·夜穹"
    assert nav.clicks == 1


def test_open_and_verify_rejects_a_different_skin():
    nav = _StubNav(["旧标题", "AS Val突击步枪·地平线"])
    finder = types.SimpleNamespace(nav=nav, _say=lambda *a, **kw: None)
    assert skinfind.SkinFinder._open_and_verify(
        finder, "AS Val突击步枪·夜穹", 300, "AS Val突击步枪·夜穹") is None


# ---------------------------------------------------------------- I6 热键
def test_hook_fire_runs_the_callback_off_the_hook_thread():
    """低级钩子线程只置标志：回调必须在别的线程里跑。"""
    done = threading.Event()
    seen = {}

    def cb():
        seen["thread"] = threading.current_thread().name
        done.set()

    hk = hotkey.HookHotkey("F8", callback=cb)
    hk._fire()
    assert done.wait(2.0), "回调没被调度"
    assert seen["thread"] == "hotkey-fire"
    assert seen["thread"] != threading.current_thread().name


def test_hook_fire_is_debounced():
    calls = []
    hk = hotkey.HookHotkey("F8", callback=lambda: calls.append(1))
    hk._fire()
    hk._fire()
    for _ in range(20):
        if calls:
            break
        threading.Event().wait(0.02)
    threading.Event().wait(0.2)
    assert len(calls) == 1


def test_hook_status_is_inactive_before_start():
    hk = hotkey.HookHotkey("F8")
    assert hk.status()["active"] is False


def test_hook_status_does_not_trust_a_stale_flag():
    """本地 flag 还是 True、但线程/句柄都没了 → 必须报「没在监听」。"""
    hk = hotkey.HookHotkey("F8")
    hk._ok = True
    hk._hook = 0x1234
    hk._thread = None
    assert hk.status()["active"] is False
